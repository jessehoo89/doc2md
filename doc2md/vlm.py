"""通用视觉模型（VLM）云端 OCR 客户端 —— 一份代码接多家平台。

## 为什么需要它

paddle 与 mineru 两条通道虽然厂商不同，但**技术形态是同一类**：都是「AI 平台的
专用文档解析服务」。它们都有任务队列、都会背压、限流窗口也偏同源，真出事时容易
一起出问题 —— 只堆同类的后端，鲁棒性提升有限。

要真正分散风险，链路里至少要有一个**形态不同的后端**：

  专用文档解析 API（paddle / mineru）→ 提交任务→轮询→取结果包，有队列、会背压
  通用 VLM 的 /chat/completions     → 一问一答，没有队列，故障模式完全不同

## 接入的模型

默认对接硅基流动的 `deepseek-ai/DeepSeek-OCR`：它是**专用 OCR 模型**（不是聊天
模型兼职做 OCR），且平台支持 PDF 直接 base64 上传，由服务端决定最优分辨率：

    {"type": "image_url",
     "image_url": {"url": "data:application/pdf;base64,..."}}

官方文档给的几套提示词都可用（见 config.OcrConfig.prompt 的注释）。
换成别的 OpenAI 兼容平台时，只需要改 base_url + model + token 三个值。

## 必须知道的两个差异

**① 不返回插图。** 它只吐 Markdown 文字，正文里的图片不会有任何引用。所以
`returns_images=False`会让引擎在首次用到它时提示「这批文件的插图位置会缺」；
需要插图时应让 paddle / mineru precision 排在它前面。

**② 输出可能被 `max_tokens` 截断。** 页数多、表格密时模型会写到一半被切断，
API 只给 `finish_reason == "length"` 这一个信号 —— 不检查就等于**静默丢内容**。
本客户端检测到就明确告警，并把它计进 `truncated` 供报告汇总。

错误分类沿用 ocr.py 的体系，路由器据此决定「熔断并切换」还是「直接判该文件失败」。
"""
from __future__ import annotations

import base64
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .ocr import (
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

DEFAULT_BASE = "https://api.siliconflow.cn/v1"

# 默认模型：硅基流动上的专用 OCR 模型（支持 PDF 直传，不是聊天模型兼职）
DEFAULT_MODEL = "deepseek-ai/DeepSeek-OCR"

# 提示词：官方文档给出的几种场景，默认用第一种（文档转 Markdown，带版面定位）
PROMPT_DOC2MD = "<image>\n<|grounding|>Convert the document to markdown."
PROMPT_OCR = "<image>\n<|grounding|>OCR this image."
PROMPT_FREE = "<image>\nFree OCR."
PROMPT_FIGURE = "<image>\nParse the figure."

# 硅基流动业务码 → 异常分类
#   50505 = Model service overloaded，是临时拥塞，退避后能好 → 当背压处理
SF_BACKPRESSURE_CODES = {50505}

# "模型名不存在"这类**配置错误**的信号词。命中即永久熔断（本次运行内不再试），
# 否则每处理一个文件都会白撞一次：它不是服务端故障，重试一万次也不会变成可用。
MODEL_MISSING_HINTS = (
    "model not found", "model does not exist", "no such model", "invalid model",
    "unknown model", "model_not_found", "模型不存在", "模型未找到", "不存在的模型",
)

# DeepSeek-OCR 的 grounding 输出形如：
#   <|ref|>标题文字<|/ref|><|det|>[[100,50,800,90]]<|/det|>
# 坐标块要去掉（md 里没用），文字要留下。
_GROUND_DET = re.compile(r"<\|det\|>.*?<\|/det\|>", re.S)
_GROUND_TAG = re.compile(r"<\|/?(?:ref|det|grounding|image)\|>")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _strip_boxed(text: str) -> str:
    """把 `\\boxed{...}` 的外壳去掉、保留里面内容（DeepSeek-OCR 常这么包表格）。

    需要配对扫描而不是简单 replace：表格里会嵌套 `{` `}`，粗暴替换会把结构打烂。
    """
    marker = "\\boxed{"
    out: list[str] = []
    i = 0
    n = len(text)
    while True:
        k = text.find(marker, i)
        if k < 0:
            out.append(text[i:])
            break
        out.append(text[i:k])
        j = k + len(marker)
        depth = 1
        while j < n and depth:
            ch = text[j]
            if ch == "\\":          # 跳过被转义的字符，避免误判花括号
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        out.append(text[k + len(marker): j - 1] if depth == 0 else text[k:])
        i = j
    return "".join(out)


def clean_vlm_markdown(text: str) -> str:
    r"""清掉视觉模型的定位标签与 \boxed{} 外壳，留下正常 Markdown。"""
    text = _GROUND_DET.sub("", text)
    text = _GROUND_TAG.sub("", text)
    text = _strip_boxed(text)
    # 模型有时会把提示词里的 <image> 原样吐回来
    text = text.replace("<image>", "")
    # 去掉因此产生的孤立空行（连续 3 个以上换行压成 2 个）
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text.strip()


class VlmOcrClient(RetryMixin):
    """OpenAI 兼容 /chat/completions 的视觉模型 OCR 后端。

    接口形态与 PaddleOcrClient / MinerUOcrClient 完全一致，因此对引擎与路由器透明。
    """

    type = "vlm"

    def __init__(self, ocr_cfg, store=None, verbose: bool = True,
                 logger: Callable[[str], None] | None = None):
        self.cfg = ocr_cfg
        self.name = getattr(ocr_cfg, "name", "vlm") or "vlm"
        self.store = store
        self.verbose = verbose
        self._logger = logger
        self.base = (self.cfg.base_url or DEFAULT_BASE).rstrip("/")
        self.model = (self.cfg.model or DEFAULT_MODEL).strip()
        self.input_mode = (getattr(self.cfg, "input_mode", "") or "pdf").strip().lower()
        if self.input_mode not in ("pdf", "image"):
            raise ValueError(
                f"VLM 的 input_mode 只能是 pdf 或 image，收到 {self.input_mode!r}"
            )
        self._local = threading.local()
        self._sem = threading.Semaphore(max(1, int(self.cfg.concurrency or 1)))
        self._pages_lock = threading.Lock()
        self._pages_submitted = 0

    # ---------- 就绪与连通性 ----------
    def ready(self) -> tuple[bool, str]:
        """是否具备运行条件。路由器据此决定要不要纳入链路。"""
        if not self.model:
            return False, "未配置 model"
        # send_token 显式设成 False 时允许无 Token（自建 vLLM 等免鉴权服务）
        if not self.cfg.token and getattr(self.cfg, "send_token", None) is not False:
            return False, ("需要 Token：到 tools\\.env 填 DOC2MD_SILICONFLOW_TOKEN"
                           "（siliconflow.cn 控制台「API 密钥」页创建）")
        return True, "就绪"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.token and getattr(self.cfg, "send_token", None) is not False:
            h["Authorization"] = f"Bearer {self.cfg.token}"
        return h

    def _endpoint(self, path: str) -> str:
        """拼端点。base_url 只写到域名时自动补 /v1，免得用户漏写成 404。"""
        base = self.base
        if not urlparse(base).path.strip("/"):
            base = base + "/v1"
        return f"{base}{path}"

    def _session(self) -> requests.Session:
        s = getattr(self._local, "sess", None)
        if s is None:
            s = requests.Session()
            self._local.sess = s
        return s

    def ping(self) -> tuple[bool, str]:
        """连通性自检。/models 不消耗额度，顺便核对模型名在不在平台模型列表里。"""
        ok, why = self.ready()
        if not ok:
            return False, why
        if not self.cfg.token:
            return True, "免鉴权模式（未配置 Token）"
        try:
            r = self._session().get(self._endpoint("/models"), headers=self._headers(),
                                    timeout=30)
            if r.status_code in (401, 403):
                return False, f"Token 无效（HTTP {r.status_code}）"
            if r.status_code >= 400:
                return True, f"服务可达（HTTP {r.status_code}，/models 不可用）"
            try:
                ids = [str(m.get("id", "")) for m in (r.json().get("data") or [])]
            except Exception:
                ids = []
            if ids and self.model not in ids:
                return True, (f"服务可达，但模型列表里没有 {self.model}"
                              f"（可能是名字写错或该模型已下线，共 {len(ids)} 个模型）")
            return True, f"服务可达（HTTP 200，模型 {self.model} 在列）"
        except Exception as e:
            return False, f"无法连接：{str(e)[:120]}"

    # ---------- 配额 ----------
    def _check_quota(self, pages: int) -> None:
        """当日配额预检。按后端名分别计数，不与其它后端互相挤占。"""
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
        return set(SF_BACKPRESSURE_CODES)

    def _permanent(self, msg: str) -> BackendUnavailable:
        """构造一个"本次运行内永久熔断"的故障（冷却 0 → open_until = FOREVER）。

        用于配置类错误：模型名不存在、路径不存在……这类问题重试不会变好，
        每处理一个文件都去撞一次纯属浪费。
        """
        err = BackendUnavailable(msg)
        err.cooldown = 0.0
        return err

    @staticmethod
    def _looks_like_model_missing(msg: str) -> bool:
        low = (msg or "").lower()
        return any(h in low for h in MODEL_MISSING_HINTS)

    def _classify_http(self, status: int, api_code, detail: str, what: str) -> OcrError | None:
        if self._looks_like_model_missing(detail) or self._looks_like_model_missing(str(api_code)):
            return self._permanent(
                f"{self.name} 模型 {self.model} 不存在或不可用（HTTP {status}）：{detail}"
            )
        if status == 404:
            # 404 多半是 base_url 少写/多写了 /v1，属配置错误
            return self._permanent(
                f"{self.name} 接口路径不存在（HTTP 404）：{detail}"
                f"　检查 base_url 是否要带 /v1（当前 {self.base}）"
            )
        return super()._classify_http(status, api_code, detail, what)

    def _classify_business(self, code, msg: str, what: str) -> OcrError | None:
        c: Any = code
        if isinstance(c, str):
            if c.isdigit():
                c = int(c)
        if isinstance(c, int):
            tag = f"{self.name} {what}（code={c}）"
            if c in SF_BACKPRESSURE_CODES:
                return BackpressureExhausted(f"{tag} 服务端过载：{msg}")
        if self._looks_like_model_missing(msg):
            return self._permanent(f"{self.name} 模型 {self.model} 不存在或不可用：{msg}")
        return None

    @staticmethod
    def _safe_json(resp) -> dict | None:
        try:
            j = resp.json()
            return j if isinstance(j, dict) else None
        except Exception:
            return None

    def _ensure_ok(self, resp, what: str) -> dict:
        """校验响应。OpenAI 兼容平台会在 HTTP 200 里塞业务错误码，必须显式看。"""
        body = self._safe_json(resp)
        if resp.status_code < 400 and body is not None and "choices" in body:
            code = body.get("code")
            if code in (None, 0):
                return body
        code = body.get("code") if body else None
        msg = ""
        if body:
            msg = str(body.get("message") or body.get("msg") or body.get("errorMsg") or "")
        exc = self._classify_business(code, msg, what) if code is not None else None
        if exc is None:
            txt = (resp.text or "")[:200]
            exc = self._classify_http(resp.status_code, code, msg or txt, what)
        if exc is not None:
            raise exc
        if resp.status_code >= 400:
            resp.raise_for_status()      # 交给基类按 HTTP 码分类与退避
        raise OcrError(
            f"{self.name} {what} 响应异常：HTTP {resp.status_code} code={code} {msg[:200]}"
        )

    # ---------- 提示词 ----------
    def _prompt_text(self) -> str:
        p = (getattr(self.cfg, "prompt", "") or "").strip()
        if p:
            return p
        if self.input_mode == "image":
            return PROMPT_OCR
        return PROMPT_DOC2MD

    # ---------- 请求 ----------
    def _chat(self, parts: list[dict], what: str) -> tuple[str, dict, bool]:
        """发一次对话请求，返回 (清洗后的 markdown, usage, 是否被截断)。"""
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": parts}],
            # 硅基流动实测：流式返回时内容格式异常，必须非流式
            "stream": False,
            "temperature": float(getattr(self.cfg, "temperature", 0.0) or 0.0),
            "max_tokens": int(getattr(self.cfg, "max_tokens", 8192) or 8192),
        }
        url = self._endpoint("/chat/completions")

        def _do():
            r = self._session().post(url, headers=self._headers(), json=body,
                                     timeout=self.cfg.request_timeout)
            return self._ensure_ok(r, what)

        payload = self._retry(_do, what)
        choices = payload.get("choices") or []
        if not choices:
            raise EmptyResultError(f"{self.name} {what} 响应里没有 choices")
        choice = choices[0] or {}
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):
            # 个别平台会把 content 拆成 [{type:text,text:...}] 分段
            content = "".join(
                seg.get("text", "") for seg in content if isinstance(seg, dict)
            )
        text = clean_vlm_markdown(str(content or ""))
        truncated = str(choice.get("finish_reason") or "") == "length"
        if truncated:
            # 输出被 max_tokens 截断 = 这段内容丢了后半截，必须让人看见。
            # 注意：不靠"正文里有没有某个词"来判断，那是猜；只看 finish_reason。
            self._log(
                f"[OCR/{self.name}] ⚠ {what} 输出被 max_tokens"
                f"（{body['max_tokens']}）截断，该段后半部分内容已丢失。"
                f"解决：调小分段页数（max_pages）或调大 max_tokens。"
            )
            text += (
                f"\n\n> ⚠️ 本段由 {self.name} 识别时因输出长度上限被截断，"
                f"内容可能不完整，建议调小分段页数后重跑。\n"
            )
        return text, (payload.get("usage") or {}), truncated

    # ---------- 输入构造 ----------
    def _slice_pdf(self, src: Path, start: int, end: int) -> bytes:
        """取出 PDF 的 [start, end] 页（1 基、闭区间），返回新的 PDF 字节。"""
        import pymupdf

        from .detect import silence_mupdf

        silence_mupdf()
        try:
            with pymupdf.open(str(src)) as doc:
                total = doc.page_count
                a = max(0, start - 1)
                b = min(total, end)
                if b <= a:
                    raise DocumentError(
                        f"{self.name} 取不到 {src.name} 的第 {start}-{end} 页"
                        f"（共 {total} 页）"
                    )
                out = pymupdf.open()
                try:
                    out.insert_pdf(doc, from_page=a, to_page=b - 1)
                    return out.tobytes()
                finally:
                    out.close()
        except DocumentError:
            raise
        except Exception as e:
            raise DocumentError(
                f"{self.name} 无法读取 {src.name} 的 PDF 内容：{str(e)[:150]}"
            ) from e

    def _page_images(self, src: Path, start: int, end: int) -> list[bytes]:
        """把 [start, end] 页渲染成 PNG 字节（整张图片源则原样返回）。"""
        if src.suffix.lower() != ".pdf":
            try:
                return [src.read_bytes()]
            except OSError as e:
                raise DocumentError(f"{self.name} 无法读取 {src.name}：{e}") from e

        import pymupdf

        from .detect import silence_mupdf

        silence_mupdf()
        dpi = int(getattr(self.cfg, "dpi", 200) or 200)
        blobs: list[bytes] = []
        try:
            with pymupdf.open(str(src)) as doc:
                total = doc.page_count
                a = max(0, start - 1)
                b = min(total, end)
                if b <= a:
                    raise DocumentError(
                        f"{self.name} 取不到 {src.name} 的第 {start}-{end} 页"
                        f"（共 {total} 页）"
                    )
                for i in range(a, b):
                    pix = doc[i].get_pixmap(dpi=dpi)
                    blobs.append(pix.tobytes("png"))
        except DocumentError:
            raise
        except Exception as e:
            raise DocumentError(
                f"{self.name} 无法渲染 {src.name}：{str(e)[:150]}"
            ) from e
        return blobs

    @staticmethod
    def _mime_of(src: Path, kind: str) -> str:
        if kind == "pdf":
            return "application/pdf"
        return _MIME.get(src.suffix.lower(), "image/png")

    def _image_part(self, blob: bytes, mime: str) -> dict:
        b64 = base64.b64encode(blob).decode("ascii")
        part: dict[str, Any] = {"type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"}}
        detail = str(getattr(self.cfg, "image_detail", "") or "").strip().lower()
        if detail in ("auto", "low", "high"):
            part["image_url"]["detail"] = detail
        return part

    # ---------- 单段转换 ----------
    def _convert_chunk(self, src: Path, start: int, end: int) -> tuple[str, int, int, int]:
        """转换一段。返回 (markdown, 请求次数, 截断次数, token 用量)。"""
        prompt = self._prompt_text()
        label = f"{src.name} 第 {start}-{end} 页" if end > start else f"{src.name} 第 {start} 页"
        texts: list[str] = []
        n_req = 0
        n_cut = 0
        tokens = 0

        if self.input_mode == "pdf" and src.suffix.lower() == ".pdf":
            blob = self._slice_pdf(src, start, end)
            parts = [self._image_part(blob, "application/pdf"),
                     {"type": "text", "text": prompt}]
            try:
                text, usage, cut = self._chat(parts, label)
            except OcrError as e:
                # 只有"未定性的通用错误"才尝试按体积/页数重新定性；
                # 已经分类好的（鉴权/背压/文档坏）原样上抛，别把分类搅浑。
                if type(e) is OcrError and _is_too_large(e):
                    raise CapabilityError(
                        f"{self.name} 不接受该 PDF 段（{label}）：{str(e)[:160]}"
                    ) from e
                raise
            n_req += 1
            tokens += int(usage.get("total_tokens") or 0)
            n_cut += int(cut)
            texts.append(text)
        else:
            blobs = self._page_images(src, start, end)
            mime = self._mime_of(src, "image")
            one_by_one = bool(getattr(self.cfg, "one_page_per_request", True))
            if one_by_one:
                for offset, blob in enumerate(blobs):
                    page_no = start + offset
                    parts = [self._image_part(blob, mime),
                             {"type": "text", "text": prompt}]
                    text, usage, cut = self._chat(parts, f"{src.name} 第 {page_no} 页")
                    n_req += 1
                    tokens += int(usage.get("total_tokens") or 0)
                    n_cut += int(cut)
                    texts.append(text)
            else:
                parts = [self._image_part(b, mime) for b in blobs]
                parts.append({"type": "text", "text": prompt})
                text, usage, cut = self._chat(parts, label)
                n_req += 1
                tokens += int(usage.get("total_tokens") or 0)
                n_cut += int(cut)
                texts.append(text)

        self._log(f"[OCR/{self.name}] {label} 完成"
                  f"（{n_req} 次请求"
                  + (f"，{tokens} tokens" if tokens else "")
                  + ("，**有截断**" if n_cut else "")
                  + "）")
        return "\n\n".join(t for t in texts if t.strip()), n_req, n_cut, tokens

    # ---------- 高层：单文件全流程 ----------
    def convert_file(self, src: Path, md_path: Path, pages: int = 1,
                     batch_id: str | None = None) -> OcrResult:
        t0 = time.time()
        try:
            size_mb = src.stat().st_size / 1048576.0
        except OSError:
            size_mb = 0.0

        if src.suffix.lower() == ".pdf":
            chunks = plan_page_chunks(self.cfg, self.name, pages or 0, size_mb, self._log)
        else:
            chunks = [(1, 1, None)]
        self._check_quota(pages or 1)

        parts: list[str] = []
        n_req = 0
        n_cut = 0
        tokens = 0
        for start, end, _page_range in chunks:
            with self._sem:
                text, r, c, tk = self._convert_chunk(src, start, end)
            n_req += r
            n_cut += c
            tokens += tk
            parts.append(text)
            with self._pages_lock:
                self._pages_submitted += max(end - start + 1, 1)
            if self.store:
                self.store.add_pages(max(end - start + 1, 1), backend=self.name)

        md = "\n\n".join(p for p in parts if p.strip())
        if not md.strip():
            raise EmptyResultError(
                f"{self.name} 返回内容为空（该模型可能不擅长此版式，可换后端再试）"
            )
        if n_cut:
            self._log(
                f"[OCR/{self.name}] ⚠ {src.name} 有 {n_cut} 段输出被截断，"
                f"该文件内容不完整，建议调小 max_pages 后重跑"
            )
        return OcrResult(
            markdown=md + "\n",
            pages=pages,
            image_count=0,          # 该形态不返回插图（见模块文档）
            elapsed=time.time() - t0,
            job_id=f"{self.name}:{n_req}req,{tokens}tok",
            backend=self.name,
            chunks=len(chunks),
        )


def _is_too_large(exc: BaseException) -> bool:
    """响应里是不是"文件/请求太大"这类信息 —— 属能力不足，不是服务端故障。"""
    msg = str(exc).lower()
    hints = ("too large", "exceed", "payload", "413", "too many", "max", "limit",
             "超过", "过大", "超出")
    return any(h in msg for h in hints)


# 便于外部按名字引用
__all__ = [
    "VlmOcrClient",
    "clean_vlm_markdown",
    "DEFAULT_BASE",
    "DEFAULT_MODEL",
    "PROMPT_DOC2MD",
    "PROMPT_OCR",
    "PROMPT_FREE",
    "PROMPT_FIGURE",
]
