"""云端 OCR 客户端基座：错误分类体系 + PaddleOCR 异步作业接口实现。

错误分类是整个"多后端熔断切换"的判定依据，见下方 OcrError 家族：

  BackendUnavailable   后端整体不可用（配额耗尽 / 鉴权失败 / 持续背压 / 网络故障）
                       → 熔断该后端 + 切换下一个后端
   ├ QuotaExceeded     当日配额用尽      （冷却 0：本次运行内不再尝试）
   ├ AuthError         鉴权失败          （冷却 0：本次运行内不再尝试）
   ├ BackpressureExhausted  提交队列已满 （冷却用后端配置，默认 300s）
   └ NetworkUnavailable 连不通/超时/5xx  （冷却用后端配置）
  CapabilityError      该后端能力不足（页数/体积/格式/参数超限）
                       → 只切换，不熔断（后端本身是健康的）
  DocumentError        文档自身的问题（内容为空 / 文件损坏）
                       → 快速失败，不切换（换谁都没用，切换只是白烧配额）
  OcrError（基类）     其它未知错误 → 不熔断但允许切换，避免单点小故障拖垮整轮

PaddleOCR 协议（已实测打通）：
  提交  POST {base}/api/v2/ocr/jobs         multipart: file + model + optionalPayload
  轮询  GET  {base}/api/v2/ocr/jobs/{jobId} state: pending|running|done|failed
  取件  GET  data.resultUrl.jsonUrl          JSONL，每行 result.layoutParsingResults[]
"""
from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests


class OcrError(RuntimeError):
    """OCR 通用错误。默认不熔断、允许切换后端（未知故障多试一个后端往往就好了）。"""

    kind = "error"
    trip = False        # 是否据此熔断该后端
    failover = True     # 是否切换下一个后端
    cooldown: float | None = None   # 熔断冷却秒数；None 用路由器默认值，0 表示当日不再尝试


class BackendUnavailable(OcrError):
    """后端整体不可用：配额耗尽、鉴权失败、持续背压、服务端故障。

    这类问题跟具体文件无关，换一个后端很可能就成功了 → 熔断 + 切换。
    """

    kind = "unavailable"
    trip = True
    failover = True


class QuotaExceeded(BackendUnavailable):
    """当日配额用尽。冷却 0 → 本次运行内不再尝试该后端（重试也是白费）。"""

    kind = "quota"
    cooldown = 0.0


class AuthError(BackendUnavailable):
    """鉴权失败（Token 无效/过期）。冷却 0 → 本次运行内不再尝试。"""

    kind = "auth"
    cooldown = 0.0


class BackpressureExhausted(BackendUnavailable):
    """服务端长期队列满，退避耗尽仍未提交成功。属"稍后再试"，不是文件本身有问题。

    冷却时间用**该后端配置的** `breaker_cooldown`（默认 300 秒），因为不同服务
    的恢复速度差别很大；半开试探若再次失败会在此基础上翻倍。
    这里显式置 None 表示"听配置的"，否则会盖掉后端的个性化设置。
    """

    kind = "backpressure"
    cooldown = None


class NetworkUnavailable(BackendUnavailable):
    """网络层故障：连不通 / 超时 / 5xx / 429 退避耗尽。

    与 BackpressureExhausted 的区别很重要（日志与统计要看得懂）：
      背压 = 对面活着，只是提交队列满了（业务码 10010），几分钟就好；
      网络 = 根本没连上或对面 5xx，可能是地址配错、本机断网、服务已下线。
    两者都该"熔断并切换下一个后端"，但不该混为一谈 ——
    否则日志里会出现"因 backpressure 熔断"而原因却是 Connection refused。
    """

    kind = "network"
    cooldown = None   # 听该后端配置的 breaker_cooldown


class CapabilityError(OcrError):
    """该后端处理不了这个文件（页数/体积超限、格式不支持、参数不适用）。

    后端本身是健康的 → 只切换后端，不熔断。
    """

    kind = "capability"
    trip = False
    failover = True


class DocumentError(OcrError):
    """文档自身的问题（内容为空 / 文件损坏 / 结果无法解析）。

    换任何后端都是同样结果 → 快速失败，不切换，省下其它后端的配额。
    """

    kind = "document"
    trip = False
    failover = False


class EmptyResultError(OcrError):
    """识别跑通了但结果为空。

    与 DocumentError 的区别：空结果**有**可能是该后端对这个版式不擅长，
    换个引擎（比如 VLM ↔ pipeline）往往就有内容，所以允许切换一次；
    但后端本身是健康的，不熔断。
    """

    kind = "empty"
    trip = False
    failover = True


# 服务端提示"任务提交队列已满"的业务码。属临时背压，必须退避重试，不能判死。
BACKPRESSURE_CODES = {10010}
BACKPRESSURE_HINTS = ("队列已满", "队列满", "queue is full", "queue full")


@dataclass
class OcrResult:
    markdown: str
    pages: int
    image_count: int
    elapsed: float
    job_id: str
    backend: str = ""          # 实际由哪个后端产出（路由器回填），便于审计与统计
    chunks: int = 1            # 分段提交的次数（页数超过单任务上限时 > 1）
    attempts: list[str] = field(default_factory=list)   # 尝试过的后端及结果，便于排障
    image_placeholders: int = 0   # md 里残留的插图占位数（后端没把图给回来时 > 0）


_IMG_SRC = re.compile(r'src="([^"]+)"')


def plan_page_chunks(cfg, name: str, pages: int, size_mb: float = 0.0,
                     log: Callable[[str], None] | None = None
                     ) -> list[tuple[int, int, str | None]]:
    """按后端上限把文件切成若干段。返回 [(起页, 止页, page_range)]。

    两个约束都要看：页数上限与体积上限。本库里有 210~420MB 的国标扫描件，
    即使页数没超也可能体积超，所以每段页数取「页数约束」与「体积约束」里更小的那个。

    公共实现：MinerU（page_ranges 分段提交）与通用 VLM（逐段送模型）共用，
    免得两处逻辑各自漂移。
    """
    max_pages = int(cfg.max_pages or 0)
    max_mb = float(cfg.max_file_mb or 0.0)
    over_pages = bool(max_pages and pages > max_pages)
    over_size = bool(max_mb and size_mb > max_mb)
    if not (over_pages or over_size):
        return [(1, pages, None)]

    detail = []
    if over_size:
        detail.append(f"体积 {size_mb:.0f}MB > {max_mb:g}MB")
    if over_pages:
        detail.append(f"页数 {pages} > {max_pages}")
    if not cfg.chunk_over_limit:
        raise CapabilityError(
            f"{name} 不接受该文件（{'、'.join(detail)}），且未开启分段提交"
        )
    if not pages:
        raise CapabilityError(
            f"{name} 不接受该文件（{'、'.join(detail)}），且页数未知无法分段"
        )

    per = pages
    if max_pages:
        per = min(per, max_pages)
    if max_mb and size_mb > 0:
        # 按体积等比折算每段能放多少页，留 5% 余量防扫描页大小不均
        est = int(pages * (max_mb * 0.95) / size_mb)
        per = min(per, max(1, est))
    per = max(1, per)

    chunks: list[tuple[int, int, str | None]] = []
    start = 1
    while start <= pages:
        end = min(start + per - 1, pages)
        chunks.append((start, end, f"{start}-{end}"))
        start = end + 1
    if log is not None:
        log(f"[OCR/{name}] 文件超限（{'、'.join(detail)}），"
            f"拆成 {len(chunks)} 段、每段 {per} 页提交")
    return chunks


class RetryMixin:
    """退避重试 + 服务端错误分类，PaddleOCR 与 MinerU 客户端共用。

    子类需要提供：`cfg`（含退避参数）、`name`（后端名）、`_session()`，
    并可按需覆盖 `_backpressure_codes()` / `_classify_business()`。

    设计要点：把"服务器/账号级故障"与"文件级故障"分得干净 ——
      背压、限流、5xx、网络抖动  → 先退避重试，耗尽后抛 BackendUnavailable 家族
      鉴权、配额、参数/体积     → 直接抛对应分类，交给路由器熔断或切换
    """

    cfg: Any
    name: str = "ocr"

    # ---------- 日志 ----------
    def _log(self, msg: str) -> None:
        logger = getattr(self, "_logger", None)
        if logger is not None:
            logger(msg)
        elif getattr(self, "verbose", False):
            print(msg, flush=True)

    # ---------- 服务端错误解析 ----------
    @staticmethod
    def _api_error(resp) -> tuple[Any, str]:
        """从错误响应里取出业务码与后端给的原因。

        各家信封不一致，都要认：
          MinerU 常规：{"code": -60003, "msg": "..."}
          MinerU 鉴权：{"msgCode": "A0202", "msg": "user authenticate failed"}
          SiliconFlow：{"code": 50505, "message": "Model service overloaded..."}
                       或裸文本 "Invalid token"
        只盯 `code` 会漏掉鉴权那套（code 缺失 → 退化成通用错误，鉴权故障被当成偶发）；
        只盯 `msg` 会把 SiliconFlow 的 `message` 整个丢掉，排障时看不到真实原因。
        """
        if resp is None:
            return None, ""
        try:
            j = resp.json()
        except Exception:
            return None, (resp.text or "")[:300]
        if isinstance(j, dict):
            code = j.get("code")
            if code is None:
                code = j.get("msgCode")
            msg = j.get("msg") or j.get("message") or j.get("errorMsg") or ""
            if code is None and isinstance(j.get("data"), dict):
                code = j["data"].get("code")
                msg = msg or j["data"].get("msg") or ""
            return code, str(msg)
        return None, (resp.text or "")[:300]

    def _backpressure_codes(self) -> set:
        """本后端表示"提交队列已满"的业务码。"""
        return set(BACKPRESSURE_CODES)

    def _is_backpressure(self, resp) -> bool:
        """是否是"提交队列已满"这类临时背压。"""
        code, msg = self._api_error(resp)
        if code in self._backpressure_codes():
            return True
        low = msg.lower()
        return any(h in msg or h in low for h in BACKPRESSURE_HINTS)

    def _classify_business(self, code, msg: str, what: str) -> OcrError | None:
        """把后端业务码翻译成分类异常。子类覆盖；返回 None 表示不认识。"""
        return None

    def _classify_http(self, status: int, api_code, detail: str, what: str) -> OcrError | None:
        """把 HTTP 状态码翻译成分类异常。返回 None 表示走通用逻辑。"""
        if status in (401, 403):
            return AuthError(f"{self.name} 鉴权失败（{status}）：{detail}")
        if status == 422:
            # 本后端的参数校验不过 —— 后端是活的，换个后端可能就收
            return CapabilityError(f"{self.name} 参数被拒（422）：{detail}")
        if status == 413:
            return CapabilityError(f"{self.name} 文件过大（413），本后端不接受：{detail}")
        return None

    def _retry(self, fn: Callable, what: str):
        """退避重试。

        区分三类可重试故障：
          - 背压（业务码 10010「提交队列已满」）：对面活着，可能持续几十分钟，
            用独立且更长的预算；耗尽后抛 BackpressureExhausted（"稍后再试"）
          - 限流 429 / 5xx / 网络抖动：用常规预算；耗尽后抛 NetworkUnavailable
          - 其余：交给 _classify_http / _classify_business 定性，或原样抛出

        前两类都是"后端级"故障（与文件无关）→ 路由器会熔断它并切到下一个后端，
        而不是把这个文件判死。
        """
        last = None
        tries = 0
        bp_tries = 0
        max_tries = max(1, self.cfg.max_retries)
        bp_budget = max(1, self.cfg.backpressure_retries)
        # 背压总时长上限，避免单个文件无限期卡住（有全局熔断兜底）
        bp_deadline = time.time() + max(60.0, self.cfg.backpressure_retries
                                        * self.cfg.backpressure_max_wait)

        while True:
            try:
                return fn()
            except (AuthError, CapabilityError, DocumentError, QuotaExceeded):
                raise
            except requests.HTTPError as e:
                resp = e.response
                code = resp.status_code if resp is not None else 0
                api_code, api_msg = self._api_error(resp)
                detail = f"{api_msg}" + (f"（code={api_code}）" if api_code else "")
                last = f"HTTP {code}: {detail}"

                classified = self._classify_http(code, api_code, detail, what)
                if classified is not None:
                    raise classified from e

                # ---- 背压分支 ----
                if self._is_backpressure(resp):
                    bp_tries += 1
                    if bp_tries > bp_budget or time.time() > bp_deadline:
                        raise BackpressureExhausted(
                            f"{self.name} {what} 服务端提交队列持续已满"
                            f"（已退避 {bp_tries - 1} 次）：{detail}"
                        ) from e
                    wait = min(
                        self.cfg.backpressure_base_wait * bp_tries + random.uniform(0, 8),
                        self.cfg.backpressure_max_wait,
                    )
                    self._log(
                        f"[OCR/{self.name}] {what} 服务端提交队列已满，{wait:.0f}s 后重试 "
                        f"(第 {bp_tries}/{bp_budget} 次)　{detail}"
                    )
                    time.sleep(wait)
                    continue

                # ---- 限流 / 服务端错误 ----
                if code in (429, 500, 502, 503, 504):
                    tries += 1
                    if tries > max_tries:
                        raise NetworkUnavailable(
                            f"{self.name} {what} 返回 HTTP {code} 重试 {tries - 1} 次仍失败：{last}"
                        ) from e
                    wait = 30.0 if code == 429 else min(2.0 ** tries + random.random(), 60)
                    self._log(
                        f"[OCR/{self.name}] {what} 返回 {code}，{wait:.0f}s 后重试 "
                        f"({tries}/{max_tries})　{detail}"
                    )
                    time.sleep(wait)
                    continue

                raise OcrError(f"{self.name} {what} HTTP {code}：{detail}") from e

            except (requests.ConnectionError, requests.Timeout) as e:
                tries += 1
                last = f"{type(e).__name__}: {str(e)[:150]}"
                if tries > max_tries:
                    raise NetworkUnavailable(
                        f"{self.name} {what} 网络异常重试 {tries - 1} 次仍失败：{last}"
                    ) from e
                wait = min(2.0 ** tries + random.random(), 30)
                self._log(
                    f"[OCR/{self.name}] {what} 网络异常，{wait:.0f}s 后重试 ({tries}/{max_tries})"
                )
                time.sleep(wait)


class PaddleOcrClient(RetryMixin):
    """带并发、重试与配额保护的 OCR 客户端。

    入参是**该后端自己的 OcrConfig**（由配置的 backends 列表逐条生成），
    不是整个 Config —— 这样每个后端各自持有 base_url/token/并发/配额，
    路由器才能把它们当成可替换的独立通道调度。
    """

    type = "paddle"

    def __init__(self, ocr_cfg, store=None, verbose: bool = True,
                 logger: Callable[[str], None] | None = None):
        self.cfg = ocr_cfg
        self.name = getattr(ocr_cfg, "name", "paddle") or "paddle"
        self.store = store
        self.verbose = verbose
        self._logger = logger
        self.base = self.cfg.base_url.rstrip("/")
        self.jobs_url = f"{self.base}/api/v2/ocr/jobs"
        self._local = threading.local()
        self._pages_lock = threading.Lock()
        self._pages_submitted = 0

    # ---------- 就绪 ----------
    def ready(self) -> tuple[bool, str]:
        """是否具备运行条件（凭据完整）。路由器据此决定要不要纳入链路。"""
        if not self.cfg.token:
            return False, ("未配置 Token：到 tools\\.env 填 "
                           "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN 或 DOC2MD_PADDLE_TOKEN")
        if not self.cfg.base_url:
            return False, "未配置 base_url"
        return True, "就绪"

    # ---------- 会话 ----------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "sess", None)
        if s is None:
            s = requests.Session()
            s.headers["Authorization"] = f"bearer {self.cfg.token}"
            self._local.sess = s
        return s

    # ---------- 配额 ----------
    def _check_quota(self, pages: int) -> None:
        """当日配额预检。

        注意：有 store 时用 `pages_used_today()`，它在每次提交后由 `add_pages()`
        累加落库，**已经包含本次运行已提交的页数**；此时绝不能再叠加
        `_pages_submitted`，否则同一批页数被算两遍。
        （曾因此把 13,735 页真实用量报成 13,735+6,268，在配额还剩 6,265 页时
        误判「配额已用尽」，白白 deferred 掉 78 个文件。）
        `_pages_submitted` 只在没有 store 时（如 ping / 临时客户端）作为兜底计数。

        配额按**后端**分别统计（`backend_usage` 表）：PaddleOCR 的 2 万页额度
        不会被 MinerU 的消耗挤占，反之亦然。
        """
        if self.cfg.daily_page_limit <= 0:
            return
        if self.store:
            used = self.store.pages_used_today(self.name)
            planned = used + pages
            detail = f"{self.name} 今日已用 {used} 页 + 本次 {pages} 页"
        else:
            with self._pages_lock:
                submitted = self._pages_submitted
            planned = submitted + pages
            detail = f"本次已提交 {submitted} 页 + 本次 {pages} 页"
        if planned > self.cfg.daily_page_limit:
            raise QuotaExceeded(
                f"超出当日配额：{detail} > 上限 {self.cfg.daily_page_limit} 页"
            )

    # ---------- 底层 HTTP ----------
    def _optional_payload(self) -> str:
        return json.dumps(
            {
                "useDocOrientationClassify": self.cfg.use_doc_orientation_classify,
                "useDocUnwarping": self.cfg.use_doc_unwarping,
                "useChartRecognition": self.cfg.use_chart_recognition,
                "prettifyMarkdown": self.cfg.prettify_markdown,
            },
            ensure_ascii=False,
        )


    def submit(self, path: Path, batch_id: str | None = None) -> str:
        def _do():
            data = {"model": self.cfg.model, "optionalPayload": self._optional_payload()}
            if batch_id:
                data["batchId"] = batch_id
            with open(path, "rb") as f:
                r = self._session().post(
                    self.jobs_url,
                    data=data,
                    files={"file": (path.name, f, "application/octet-stream")},
                    timeout=self.cfg.request_timeout,
                )
            r.raise_for_status()
            payload = r.json()
            job_id = (payload.get("data") or {}).get("jobId")
            if not job_id:
                raise OcrError(f"提交未返回 jobId: {json.dumps(payload, ensure_ascii=False)[:300]}")
            return job_id

        return self._retry(_do, f"提交 {path.name}")

    def poll(self, job_id: str, on_progress: Callable[[str], None] | None = None) -> tuple[dict, float]:
        t0 = time.time()
        last_state = ""

        def _once():
            r = self._session().get(f"{self.jobs_url}/{job_id}", timeout=self.cfg.request_timeout)
            r.raise_for_status()
            return r.json().get("data") or {}

        while True:
            if time.time() - t0 > self.cfg.job_timeout:
                raise OcrError(f"任务 {job_id} 轮询超时（{self.cfg.job_timeout:.0f}s）")
            data = self._retry(_once, f"轮询 {job_id}")
            state = data.get("state", "")
            if state != last_state:
                last_state = state
                if on_progress:
                    on_progress(state)
            if state == "done":
                return data, time.time() - t0
            if state == "failed":
                raise OcrError(f"任务失败：{data.get('errorMsg', '未知原因')}")
            time.sleep(self.cfg.poll_interval)

    def fetch_markdown(self, data: dict, md_path: Path) -> tuple[str, int]:
        """取 JSONL 结果，拼 Markdown，并把图片落盘到 .assets。返回 (md, 图片数)。"""
        json_url = ((data.get("resultUrl") or {}).get("jsonUrl") or "").strip()
        if not json_url:
            raise OcrError(f"任务完成但没有 jsonUrl：{json.dumps(data, ensure_ascii=False)[:300]}")

        def _get():
            r = requests.get(json_url, timeout=self.cfg.request_timeout)
            r.raise_for_status()
            return r.text

        text = self._retry(_get, "下载结果")
        assets_dir = md_path.with_suffix(".assets")
        parts: list[str] = []
        img_count = 0
        page_no = 0

        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            result = item.get("result", item)
            for res in result.get("layoutParsingResults", []):
                page_no += 1
                md_obj = res.get("markdown") or {}
                page_md = md_obj.get("text") or ""
                images = md_obj.get("images") or {}
                if images:
                    img_count += self._save_images(images, assets_dir)
                # 重写图片引用路径到 assets 子目录
                for rel in images:
                    page_md = page_md.replace(f'src="{rel}"', f'src="{assets_dir.name}/{rel}"')
                parts.append(page_md)
        md = "\n\n".join(p for p in parts if p.strip())
        return md, img_count

    def _save_images(self, images: dict, assets_dir: Path) -> int:
        if not self.cfg.save_ocr_images:
            return 0
        saved = 0
        for rel, blob in images.items():
            try:
                target = assets_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                data = self._decode_image(blob)
                if data:
                    target.write_bytes(data)
                    saved += 1
            except Exception as e:  # 单张图失败不影响整体
                self._log(f"[OCR] 图片保存失败 {rel}: {str(e)[:80]}")
        return saved

    def _retry_download(self, url: str) -> bytes:
        def _get():
            r = requests.get(url, timeout=self.cfg.request_timeout)
            r.raise_for_status()
            return r.content

        return self._retry(_get, "下载图片")

    def _decode_image(self, blob: str) -> bytes | None:
        if not blob:
            return None
        if isinstance(blob, bytes):
            return blob
        if blob.startswith("http://") or blob.startswith("https://"):
            return self._retry_download(blob)
        if blob.startswith("data:"):
            _, _, b64 = blob.partition(",")
            return base64.b64decode(b64)
        try:
            return base64.b64decode(blob)
        except Exception:
            return None

    # ---------- 高层：单文件全流程 ----------
    def convert_file(self, src: Path, md_path: Path, pages: int = 1,
                     batch_id: str | None = None) -> OcrResult:
        t0 = time.time()
        self._check_quota(pages)
        job_id = self.submit(src, batch_id=batch_id)
        with self._pages_lock:
            self._pages_submitted += max(pages, 1)
        if self.store:
            self.store.add_pages(pages, backend=self.name)
        self._log(f"[OCR/{self.name}] {src.name} 已提交 jobId={job_id} ({pages} 页)，等待解析…")
        data, _ = self.poll(job_id)
        md, imgs = self.fetch_markdown(data, md_path)
        if not md.strip():
            raise EmptyResultError(
                f"{self.name} 返回内容为空（该后端可能不擅长此版式，可换后端再试）"
            )
        return OcrResult(
            markdown=md + "\n",
            pages=pages,
            image_count=imgs,
            elapsed=time.time() - t0,
            job_id=job_id,
            backend=self.name,
        )

    def ping(self) -> tuple[bool, str]:
        """连通性自检。"""
        if not self.cfg.token:
            return False, "未配置 Token"
        try:
            r = self._session().get(f"{self.jobs_url}/0", timeout=30)
            if r.status_code in (401, 403):
                return False, f"Token 无效（{r.status_code}）"
            return True, f"服务可达（HTTP {r.status_code}）"
        except Exception as e:
            return False, f"无法连接：{str(e)[:120]}"
