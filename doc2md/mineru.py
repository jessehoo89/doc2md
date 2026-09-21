"""MinerU 云端 OCR 客户端（官方接口文档实现）。

MinerU 提供两套接口，本客户端两种都支持，通过 `mode` 区分：

  precision —— Precision Extract API（需 Token）
      POST {base}/api/v4/file-urls/batch         申请上传地址（本地文件走这条；≤50 个/请求）
      PUT  {file_urls[i]}                        直传文件（无需 Content-Type）
      GET  {base}/api/v4/extract-results/batch/{batch_id}   轮询
      → full_zip_url 是 zip，内含 full.md + images/，版式与图片都保真
      上限 200MB / 200 页，账号每日 1000 页最高优先级（超出转低优先级）

  agent —— Agent Lightweight Extract API（免 Token，按 IP 限流）
      POST {base}/api/v1/agent/parse/file        取 task_id + OSS 签名上传地址
      PUT  {file_url}                            直传文件
      GET  {base}/api/v1/agent/parse/{task_id}   轮询
      → markdown_url 指向 CDN 上的 full.md（图片是远程链接，本客户端会下载回本地）
      上限 10MB / 20 页

两个模式的产出一致：Markdown 字符串 + 图片落盘到同名 `.assets` 目录，
因此对引擎和路由器完全透明，可以像 PaddleOCR 一样被当作可替换后端。

错误分类沿用 ocr.py 的体系：把 MinerU 的业务码翻译成
QuotaExceeded / AuthError / BackpressureExhausted / CapabilityError / DocumentError，
路由器据此决定"熔断并切换"还是"直接判该文件失败"。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

from . import converters
from .ocr import (
    AuthError,
    BackendUnavailable,
    BackpressureExhausted,
    CapabilityError,
    DocumentError,
    EmptyResultError,
    OcrError,
    OcrResult,
    QuotaExceeded,
    RetryMixin,
    plan_page_chunks,
)

DEFAULT_BASE = "https://mineru.net"

# ---- 业务码 → 异常分类（见接口文档"Common Error Codes" / "Agent-Specific Error Codes"）----
MINERU_AUTH_CODES = {"A0202", "A0211"}                     # Token 无效 / 过期
MINERU_QUOTA_CODES = {-60018, -60019}                      # 当日额度用尽
MINERU_BACKPRESSURE_CODES = {-60009}                       # 任务提交队列已满
MINERU_UNAVAILABLE_CODES = {                               # 服务端临时故障，换个后端接着干
    -10001,   # Service error
    -60001,   # 生成上传地址失败
    -60007,   # 模型服务临时不可用
    -60008,   # 文件读取超时
    -60011,   # 未取到有效文件（上传没落地）
    -60020,   # 文件拆分失败
    -60021,   # 读取页数失败
}
MINERU_CAPABILITY_CODES = {                                # 本后端接不了这个文件 → 换后端
    -500,     # 参数错误
    -10002,   # 请求参数错误
    -60002,   # 文件格式不匹配
    -60005,   # 超过体积上限
    -60006,   # 超过页数上限
    -30001,   # 超过轻量接口体积上限 10MB
    -30002,   # 轻量接口不支持该类型
    -30003,   # 超过轻量接口页数上限 20 页
    -30004,   # 轻量接口参数错误
}
MINERU_DOCUMENT_CODES = {                                  # 文件自身的问题 → 快速失败
    -60003,   # 文件读取失败（损坏）
    -60004,   # 空文件
    -60015,   # 文件转换失败
    -60016,   # 转成目标格式失败
}

# 图片引用：Markdown 语法与内嵌 HTML 两种都要认
_IMG_MD = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+?)(\s+\"[^\"]*\")?\s*\)")
_IMG_HTML = re.compile(r"(<img[^>]*?\bsrc\s*=\s*\")([^\"]+)(\")")
# MinerU 的 Markdown 常把图片写成 <!-- image--> 这样的占位注释，
# 不给出任何图片链接（实测 agent 接口 100% 如此，precision 的 full.md 也可能）。
_IMG_PLACEHOLDER = re.compile(r"<!--\s*image\s*-->", re.I)
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(ref: str) -> str:
    """把引用路径压成一个安全的本地文件名（保留扩展名）。"""
    path = urlparse(ref).path if ref.startswith(("http://", "https://")) else ref
    base = unquote(Path(path).name) or "image"
    base = _BAD_NAME.sub("_", base).strip() or "image"
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:100] + dot + ext) if dot else base[:120]
    return base


class MinerUOcrClient(RetryMixin):
    """MinerU 云端 OCR 客户端，接口形态与 PaddleOcrClient 保持一致。"""

    type = "mineru"

    def __init__(self, ocr_cfg, store=None, verbose: bool = True,
                 logger: Callable[[str], None] | None = None):
        self.cfg = ocr_cfg
        self.name = getattr(ocr_cfg, "name", "mineru") or "mineru"
        self.mode = (getattr(ocr_cfg, "mode", "") or "agent").strip().lower()
        if self.mode not in ("precision", "agent"):
            raise ValueError(f"MinerU 模式只能是 precision 或 agent，收到 {self.mode!r}")
        self.store = store
        self.verbose = verbose
        self._logger = logger
        self.base = (self.cfg.base_url or DEFAULT_BASE).rstrip("/")
        self._local = threading.local()
        self._sem = threading.Semaphore(max(1, int(self.cfg.concurrency or 1)))
        self._pages_lock = threading.Lock()
        self._pages_submitted = 0

    # ---------- 就绪与连通性 ----------
    def _use_token(self) -> bool:
        """本次请求是否要带 Authorization 头。

        轻量接口（agent）文档明确「No Authorization header required」，而且实测
        带任何无效 Token 都会被判 401 A0202 —— 白白把一个健康后端打成永久熔断。
        所以默认策略：precision 发、agent 不发；`send_token` 可显式覆盖。
        """
        if not self.cfg.token:
            return False
        explicit = getattr(self.cfg, "send_token", None)
        if explicit is not None:
            return bool(explicit)
        return self.mode == "precision"

    def ready(self) -> tuple[bool, str]:
        """是否具备运行条件（凭据/参数完整）。路由器据此决定要不要纳入链路。"""
        if self.mode == "precision" and not self.cfg.token:
            return False, ("precision 需要 Token：到 tools\\.env 填 DOC2MD_MINERU_TOKEN"
                           "（mineru.net 的 API 管理页创建）")
        language = (self.cfg.language or "ch").strip()
        if not language:
            return False, "未配置 language"
        return True, "就绪"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._use_token():
            h["Authorization"] = f"Bearer {self.cfg.token}"
        return h

    def _session(self) -> requests.Session:
        s = getattr(self._local, "sess", None)
        if s is None:
            s = requests.Session()
            self._local.sess = s
        return s

    def ping(self) -> tuple[bool, str]:
        """连通性自检。不消耗额度，只探接口可达性与 Token 有效性。"""
        ok, why = self.ready()
        if not ok:
            return False, why
        try:
            if self.mode == "agent":
                url = f"{self.base}/api/v1/agent/parse/00000000-0000-0000-0000-000000000000"
                r = self._session().get(url, timeout=30)
                body = self._safe_json(r)
                code = body.get("code") if body else None
                if code in MINERU_AUTH_CODES:
                    return False, f"鉴权失败（code={code}）"
                return True, f"服务可达（HTTP {r.status_code}）"
            url = f"{self.base}/api/v4/extract-results/batch/0"
            r = self._session().get(url, headers=self._headers(), timeout=30)
            body = self._safe_json(r)
            code = body.get("code") if body else None
            if r.status_code in (401, 403) or code in MINERU_AUTH_CODES:
                return False, f"Token 无效（HTTP {r.status_code} code={code}）"
            return True, f"服务可达（HTTP {r.status_code}）"
        except Exception as e:
            return False, f"无法连接：{str(e)[:120]}"

    # ---------- 配额 ----------
    def _check_quota(self, pages: int) -> None:
        """当日配额预检。按后端名分别计数，不与其它后端互相挤占。

        口径与 PaddleOcrClient._check_quota 保持一致：有 store 时以落库值为准，
        绝不再叠加内存里的 `_pages_submitted`（否则同一批页数算两遍）。
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
            detail = f"{self.name} 本次已提交 {submitted} 页 + 本次 {pages} 页"
        if planned > self.cfg.daily_page_limit:
            raise QuotaExceeded(
                f"{self.name} 超出当日配额：{detail} > 上限 {self.cfg.daily_page_limit} 页"
            )

    # ---------- 错误分类 ----------
    def _backpressure_codes(self) -> set:
        return set(MINERU_BACKPRESSURE_CODES)

    def _classify_business(self, code, msg: str, what: str) -> OcrError | None:
        c: Any = code
        if isinstance(c, str):
            if c in MINERU_AUTH_CODES:
                return AuthError(f"{self.name} 鉴权失败（{c}）：{msg}")
            try:
                c = int(c)
            except (TypeError, ValueError):
                return None
        if not isinstance(c, int):
            return None
        tag = f"{self.name} {what}（code={c}）"
        if c in MINERU_QUOTA_CODES:
            return QuotaExceeded(f"{tag} 当日额度已用尽：{msg}")
        if c in MINERU_BACKPRESSURE_CODES:
            return BackpressureExhausted(f"{tag} 任务提交队列已满：{msg}")
        if c in MINERU_CAPABILITY_CODES:
            return CapabilityError(f"{tag} 本后端接不了该文件：{msg}")
        if c in MINERU_DOCUMENT_CODES:
            return DocumentError(f"{tag} 文件本身有问题：{msg}")
        if c in MINERU_UNAVAILABLE_CODES:
            return BackendUnavailable(f"{tag} 服务端临时故障：{msg}")
        return None

    @staticmethod
    def _safe_json(resp) -> dict | None:
        try:
            j = resp.json()
            return j if isinstance(j, dict) else None
        except Exception:
            return None

    def _ensure_ok(self, resp, what: str) -> dict:
        """MinerU 会在 HTTP 200 里返回非 0 业务码，必须显式校验。

        两套信封都要认：
          常规   {"code": -60003, "msg": "..."}            → 看 code
          鉴权   {"msgCode": "A0202", "success": false}    → 看 msgCode
        """
        body = self._safe_json(resp)
        code = body.get("code") if body else None
        msg_code = body.get("msgCode") if body else None
        key = code if code is not None else msg_code
        ok = resp.status_code < 400 and (
            code == 0 or (code is None and body is not None and body.get("success") is True)
        )
        if ok:
            return body or {}
        msg = ""
        if body:
            msg = str(body.get("msg") or body.get("message") or body.get("errorMsg") or "")
        exc = self._classify_business(key, msg, what) if key is not None else None
        if exc is not None:
            raise exc
        if resp.status_code >= 400:
            resp.raise_for_status()          # 交给基类按 HTTP 码分类与退避
        raise OcrError(
            f"{self.name} {what} 响应异常：HTTP {resp.status_code} code={key} {msg[:200]}"
        )

    # ---------- 上传 ----------
    def _put_upload(self, url: str, src: Path) -> None:
        """PUT 直传文件到签名地址（接口要求：不带 Content-Type）。"""

        def _do():
            with open(src, "rb") as f:
                r = requests.put(url, data=f, timeout=self.cfg.request_timeout)
            if r.status_code in (200, 201, 204):
                return True
            if r.status_code >= 500:
                r.raise_for_status()
            raise BackendUnavailable(
                f"{self.name} 上传失败 HTTP {r.status_code}（签名地址可能已过期，可重新申请）"
            )

        self._retry(_do, f"上传 {src.name}")

    # ---------- precision 模式 ----------
    def _precision_submit(self, src: Path, page_range: str | None) -> tuple[str, str, str]:
        """申请批量上传地址并提交任务。返回 (batch_id, file_url, data_id)。"""
        data_id = hashlib.md5(
            f"{src}|{page_range or ''}|{time.time()}".encode("utf-8")
        ).hexdigest()[:24]
        entry: dict[str, Any] = {"name": src.name, "data_id": data_id}
        if self.cfg.is_ocr:
            entry["is_ocr"] = True
        if page_range:
            entry["page_ranges"] = page_range
        body = {
            "files": [entry],
            "model_version": (self.cfg.model or "vlm"),
            "language": self.cfg.language or "ch",
            "enable_table": bool(self.cfg.enable_table),
            "enable_formula": bool(self.cfg.enable_formula),
        }

        url = f"{self.base}/api/v4/file-urls/batch"

        def _do():
            r = self._session().post(url, headers=self._headers(), json=body,
                                     timeout=self.cfg.request_timeout)
            payload = self._ensure_ok(r, "申请上传地址")
            data = payload.get("data") or {}
            batch_id = data.get("batch_id")
            urls = data.get("file_urls") or []
            if not batch_id or not urls:
                raise OcrError(
                    f"{self.name} 未返回 batch_id/file_urls："
                    f"{json.dumps(payload, ensure_ascii=False)[:300]}"
                )
            return batch_id, urls[0], data_id

        return self._retry(_do, f"提交 {src.name}")

    def _precision_poll(self, batch_id: str, file_name: str, data_id: str,
                        page_range: str | None) -> str:
        """轮询批量结果，返回 full_zip_url。"""
        url = f"{self.base}/api/v4/extract-results/batch/{batch_id}"
        t0 = time.time()
        last = ""
        while True:
            if time.time() - t0 > self.cfg.job_timeout:
                raise BackendUnavailable(
                    f"{self.name} 任务 {batch_id} 轮询超时（{self.cfg.job_timeout:.0f}s）"
                )

            def _do():
                r = self._session().get(url, headers=self._headers(),
                                        timeout=self.cfg.request_timeout)
                return self._ensure_ok(r, "查询结果")

            payload = self._retry(_do, f"轮询 {file_name}")
            results = (payload.get("data") or {}).get("extract_result") or []
            item = None
            for it in results:
                if it.get("data_id") == data_id or it.get("file_name") == file_name:
                    item = it
                    break
            if item is None and results:
                item = results[0]
            if item is None:
                state = "pending"
            else:
                state = str(item.get("state") or "")
            if state and state != last:
                last = state
                self._log(f"[OCR/{self.name}] {file_name} 状态：{state}"
                          + (f"（{page_range}）" if page_range else ""))
            if state == "done":
                zip_url = item.get("full_zip_url") or ""
                if not zip_url:
                    raise OcrError(f"{self.name} 任务完成但没有 full_zip_url")
                return zip_url
            if state == "failed":
                err = str(item.get("err_msg") or "未知原因")
                exc = self._classify_business(item.get("err_code"), err, "解析失败")
                if exc is not None:
                    raise exc
                raise OcrError(f"{self.name} 任务失败：{err}")
            time.sleep(self.cfg.poll_interval)

    def _download_zip(self, zip_url: str) -> tuple[str, dict[str, bytes]]:
        """下载结果 zip，返回 (full.md 文本, {zip 内相对路径: 图片字节})。"""

        def _get():
            r = requests.get(zip_url, timeout=self.cfg.request_timeout)
            r.raise_for_status()
            return r.content

        blob = self._retry(_get, "下载结果包")
        md_text = ""
        images: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = zf.namelist()
                md_names = [n for n in names if Path(n).name.lower() == "full.md"]
                if md_names:
                    md_names.sort(key=len)
                    md_text = zf.read(md_names[0]).decode("utf-8", errors="replace")
                for n in names:
                    if n.endswith("/") or Path(n).name.lower() == "full.md":
                        continue
                    suffix = Path(n).suffix.lower()
                    if suffix in (".md", ".json", ".txt", ".docx", ".html", ".latex"):
                        continue
                    try:
                        images[n] = zf.read(n)
                    except Exception:
                        continue
        except zipfile.BadZipFile as e:
            raise BackendUnavailable(f"{self.name} 结果包无法解压：{str(e)[:120]}") from e
        if not md_text.strip():
            raise DocumentError(f"{self.name} 结果包里没有 full.md 或内容为空")
        return md_text, images

    # ---------- agent 模式 ----------
    def _agent_submit(self, src: Path, page_range: str | None) -> tuple[str, str]:
        """申请签名上传地址。返回 (task_id, file_url)。"""
        body: dict[str, Any] = {
            "file_name": src.name,
            "language": self.cfg.language or "ch",
            "enable_table": bool(self.cfg.enable_table),
            "enable_formula": bool(self.cfg.enable_formula),
            "is_ocr": bool(self.cfg.is_ocr),
        }
        if page_range:
            body["page_range"] = page_range
        url = f"{self.base}/api/v1/agent/parse/file"

        def _do():
            r = self._session().post(url, headers=self._headers(), json=body,
                                     timeout=self.cfg.request_timeout)
            payload = self._ensure_ok(r, "申请上传地址")
            data = payload.get("data") or {}
            task_id = data.get("task_id")
            file_url = data.get("file_url")
            if not task_id or not file_url:
                raise OcrError(
                    f"{self.name} 未返回 task_id/file_url："
                    f"{json.dumps(payload, ensure_ascii=False)[:300]}"
                )
            return task_id, file_url

        return self._retry(_do, f"提交 {src.name}")

    def _agent_poll(self, task_id: str, file_name: str, page_range: str | None) -> str:
        """轮询轻量接口，返回 markdown_url。"""
        url = f"{self.base}/api/v1/agent/parse/{task_id}"
        t0 = time.time()
        last = ""
        while True:
            if time.time() - t0 > self.cfg.job_timeout:
                raise BackendUnavailable(
                    f"{self.name} 任务 {task_id} 轮询超时（{self.cfg.job_timeout:.0f}s）"
                )

            def _do():
                r = self._session().get(url, headers=self._headers(),
                                        timeout=self.cfg.request_timeout)
                return self._ensure_ok(r, "查询结果")

            payload = self._retry(_do, f"轮询 {file_name}")
            data = payload.get("data") or {}
            state = str(data.get("state") or "")
            if state and state != last:
                last = state
                self._log(f"[OCR/{self.name}] {file_name} 状态：{state}"
                          + (f"（{page_range}）" if page_range else ""))
            if state == "done":
                # 轻量接口会把图片留成远程链接，交给 _localize_images 下载回本地
                return str(data.get("markdown_url") or "")
            if state == "failed":
                err = str(data.get("err_msg") or "未知原因")
                exc = self._classify_business(data.get("err_code"), err, "解析失败")
                if exc is not None:
                    raise exc
                raise OcrError(f"{self.name} 任务失败：{err}")
            time.sleep(self.cfg.poll_interval)

    # ---------- 图片本地化 ----------
    def _localize_images(self, md: str, md_path: Path,
                         get_blob: Callable[[str], bytes | None],
                         prefix: str = "") -> tuple[str, int]:
        """把 md 里的图片引用落盘到同名 .assets，并改写成相对路径。

        引擎要求插图是本地文件（用户明确"我需要插图"），所以：
          - precision 模式：图片来自结果 zip，直接写盘
          - agent 模式：图片是 CDN 链接，先下载再写盘
        """
        if not self.cfg.save_ocr_images:
            return md, 0
        assets = converters.assets_dir_for(md_path)
        refs = list(dict.fromkeys(list(_iter_refs(md))))
        mapping: dict[str, str] = {}
        used: set[str] = set()
        saved = 0
        for ref in refs:
            if ref.startswith("data:"):
                continue
            blob = get_blob(ref)
            if not blob:
                self._log(f"[OCR/{self.name}] 图片未取到，保留原引用：{ref[:100]}")
                continue
            name = prefix + _safe_name(ref)
            stem, dot, ext = name.rpartition(".")
            n = 1
            while name.lower() in used:
                n += 1
                name = f"{stem}_{n}{dot}{ext}" if dot else f"{name}_{n}"
            used.add(name.lower())
            try:
                target = assets / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            except OSError as e:
                self._log(f"[OCR/{self.name}] 图片写入失败 {name}：{str(e)[:80]}")
                continue
            mapping[ref] = f"{assets.name}/{name}"
            saved += 1
        if mapping:
            md = _IMG_MD.sub(
                lambda m: f"![{m.group(1)}]({mapping.get(m.group(2), m.group(2))})", md
            )
            md = _IMG_HTML.sub(
                lambda m: f"{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}", md
            )
        return md, saved

    def _zip_blob_getter(self, images: dict[str, bytes]) -> Callable[[str], bytes | None]:
        by_base = {Path(k).name.lower(): v for k, v in images.items()}

        def get(ref: str) -> bytes | None:
            for key in (ref, ref.lstrip("/"), ref.lstrip("./")):
                if key in images:
                    return images[key]
            base = Path(unquote(ref)).name.lower()
            if base in by_base:
                return by_base[base]
            # zip 里可能把图片放在 images/ 子目录，md 里也写 images/xxx.jpg
            for k, v in images.items():
                if k.lower().endswith("/" + base):
                    return v
            return None

        return get

    def _restore_placeholder_images(self, md: str, md_path: Path,
                                    images: dict[str, bytes],
                                    prefix: str) -> tuple[str, int]:
        """把 `<!-- image-->` 占位符换成真图片。

        MinerU 的 Markdown 只为插图留一个注释占位，不给链接；图片本体只存在于
        结果 zip 里。占位符是按文档顺序排的，zip 里的图片也是按抽取顺序排的，
        所以在**数量完全一致**时才做按序一一替换 —— 数量对不上宁可不插图，
        免得把图片塞到错误的位置上误导读者。
        """
        if not self.cfg.save_ocr_images:
            return md, 0
        holders = _IMG_PLACEHOLDER.findall(md)
        if not holders:
            return md, 0
        if not images:
            self._log(f"[OCR/{self.name}] md 里有 {len(holders)} 处插图占位，"
                      f"但结果里没有图片文件，插图未还原（该接口不返回图片）")
            return md, 0
        if len(images) != len(holders):
            self._log(f"[OCR/{self.name}] 插图占位 {len(holders)} 处与结果图片 "
                      f"{len(images)} 张数量不符，为免错位不做替换")
            return md, 0

        assets = converters.assets_dir_for(md_path)
        ordered = sorted(images.items())
        used: set[str] = set()
        repl: list[str] = []
        for idx, (key, blob) in enumerate(ordered, 1):
            name = f"{prefix}{idx:03d}_{_safe_name(key)}"
            stem, dot, ext = name.rpartition(".")
            n = 1
            while name.lower() in used:
                n += 1
                name = f"{stem}_{n}{dot}{ext}" if dot else f"{name}_{n}"
            used.add(name.lower())
            try:
                target = assets / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            except OSError as e:
                self._log(f"[OCR/{self.name}] 插图写入失败 {name}：{str(e)[:80]}")
                continue
            repl.append(f"![{Path(key).stem}]({assets.name}/{name})")

        it = iter(repl)
        md, n_sub = _IMG_PLACEHOLDER.subn(lambda m: next(it, m.group(0)), md)
        self._log(f"[OCR/{self.name}] 按占位符顺序还原了 {n_sub} 张插图")
        return md, n_sub

    def _remote_blob_getter(self) -> Callable[[str], bytes | None]:
        def get(ref: str) -> bytes | None:
            if not ref.startswith(("http://", "https://")):
                return None

            def _do():
                r = requests.get(ref, timeout=self.cfg.request_timeout)
                r.raise_for_status()
                return r.content

            return self._retry(_do, "下载图片")

        return get

    # ---------- 分段 ----------
    def _plan_chunks(self, pages: int, size_mb: float = 0.0
                     ) -> list[tuple[int, int, str | None]]:
        """按后端上限切段（页数与体积双约束）。实现见 ocr.plan_page_chunks。"""
        return plan_page_chunks(self.cfg, self.name, pages, size_mb, self._log)

    # ---------- 高层：单文件全流程 ----------
    def convert_file(self, src: Path, md_path: Path, pages: int = 1,
                     batch_id: str | None = None) -> OcrResult:
        t0 = time.time()
        try:
            size_mb = src.stat().st_size / 1048576.0
        except OSError:
            size_mb = 0.0
        chunks = self._plan_chunks(pages or 0, size_mb)
        self._check_quota(pages or 1)

        parts: list[str] = []
        image_total = 0
        lost_placeholders = 0
        job_refs: list[str] = []
        for idx, (start, end, page_range) in enumerate(chunks):
            prefix = "" if len(chunks) == 1 else f"p{start}_"
            md_part, imgs, ref, lost = self._convert_chunk(
                src, md_path, page_range, prefix
            )
            parts.append(md_part)
            image_total += imgs
            lost_placeholders += lost
            job_refs.append(ref)
            with self._pages_lock:
                self._pages_submitted += max(end - start + 1, 1)
            if self.store:
                self.store.add_pages(max(end - start + 1, 1), backend=self.name)

        md = "\n\n".join(p for p in parts if p.strip())
        if not md.strip():
            raise EmptyResultError(
                f"{self.name} 返回内容为空（该后端可能不擅长此版式，可换后端再试）"
            )
        return OcrResult(
            markdown=md + "\n",
            pages=pages,
            image_count=image_total,
            elapsed=time.time() - t0,
            job_id=",".join(job_refs),
            backend=self.name,
            chunks=len(chunks),
            image_placeholders=lost_placeholders,
        )

    def _convert_chunk(self, src: Path, md_path: Path, page_range: str | None,
                       prefix: str) -> tuple[str, int, str, int]:
        """返回 (markdown, 落盘图片数, 任务标识, 未还原的插图占位数)。"""
        with self._sem:
            if self.mode == "agent":
                task_id, upload_url = self._agent_submit(src, page_range)
                self._put_upload(upload_url, src)
                self._log(
                    f"[OCR/{self.name}] {src.name} 已上传 task={task_id}"
                    + (f"（{page_range}）" if page_range else "")
                    + "，等待解析…"
                )
                md_url = self._agent_poll(task_id, src.name, page_range)
                if not md_url:
                    raise BackendUnavailable(f"{self.name} 任务完成但没有 markdown_url")
                md = self._download_text(md_url)
                md, imgs = self._localize_images(
                    md, md_path, self._remote_blob_getter(), prefix
                )
                # 轻量接口只回 Markdown，图片一律不回；占位符如实报出来
                lost = len(_IMG_PLACEHOLDER.findall(md))
                if lost:
                    self._log(
                        f"[OCR/{self.name}] 该接口只返回文字，md 里有 {lost} 处 "
                        f"<!-- image--> 插图占位未能还原（要插图请配 MinerU Token 用 "
                        f"precision 模式，或让主后端处理）"
                    )
                return md, imgs, task_id, lost

            batch, upload_url, data_id = self._precision_submit(src, page_range)
            self._put_upload(upload_url, src)
            self._log(
                f"[OCR/{self.name}] {src.name} 已上传 batch={batch}"
                + (f"（{page_range}）" if page_range else "")
                + "，等待解析…"
            )
            zip_url = self._precision_poll(batch, src.name, data_id, page_range)
            md, blobs = self._download_zip(zip_url)
            # 先按 md 里的图片链接本地化（full.md 带链接时）
            md, imgs = self._localize_images(
                md, md_path, self._zip_blob_getter(blobs), prefix
            )
            # 再处理 <!-- image--> 占位（full.md 只留占位、图片在 zip 里的情况）
            md, more = self._restore_placeholder_images(md, md_path, blobs, prefix)
            lost = len(_IMG_PLACEHOLDER.findall(md))
            return md, imgs + more, batch, lost

    def _download_text(self, url: str) -> str:
        def _do():
            r = requests.get(url, timeout=self.cfg.request_timeout)
            r.raise_for_status()
            return r.text

        return self._retry(_do, "下载 Markdown")


def _iter_refs(md: str):
    for m in _IMG_MD.finditer(md):
        yield m.group(2)
    for m in _IMG_HTML.finditer(md):
        yield m.group(2)
