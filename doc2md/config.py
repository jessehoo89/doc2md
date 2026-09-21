"""配置加载：读取 config.json，支持环境变量覆盖 Token。"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PKG_DIR = Path(__file__).resolve().parent
TOOL_DIR = PKG_DIR.parent
DEFAULT_CONFIG = TOOL_DIR / "config.json"

# 各后端类型的默认服务地址。type 与 ocr 段本身不同时用它兜底，
# 避免把 A 厂商的地址继承给 B 厂商的后端。
DEFAULT_BASE_URL = {
    "paddle": "https://paddleocr.aistudio-app.com",
    "mineru": "https://mineru.net",
    # 通用 OpenAI 兼容视觉模型（/chat/completions）。默认指向硅基流动 ——
    # 它上面有 deepseek-ai/DeepSeek-OCR 这个**专用 OCR 模型**，而且支持 PDF
    # 直接 base64 上传。换供应商只需要改 base_url + model + token 三个值。
    "vlm": "https://api.siliconflow.cn/v1",
}

# 凭据文件：与本文件同目录的 .env，集中存放各云端 OCR 的 Token。
# 这样 Token 不必写进 config.json（config.json 常被分享/备份/贴日志）。
# 可用环境变量 DOC2MD_ENV_FILE 指向别处；设为 none/off/0/- 表示完全不读该文件。
ENV_FILE = TOOL_DIR / ".env"
_ENV_FILE_OFF = {"none", "off", "0", "-", "false", "no"}

# 记录"哪些环境变量是本进程自己从 .env 灌进去的"。下次再读同一个 .env 时，
# 这些键允许被文件里的新值覆盖（改了 Token 不必重启）；而外部真实设置的环境
# 变量始终优先，绝不会被文件覆盖。
_ENV_FILE_KEYS: set[str] = set()


def _parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本：KEY=VALUE，支持 # 注释、export 前缀、引号包裹。

    容错优先：认不出的行直接跳过，不抛异常（这份文件是给人手写的）。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]                      # 引号包裹：原样取值
        elif " #" in val:                        # 未加引号：去掉行尾注释
            val = val.split(" #", 1)[0].rstrip()
        out[key] = val
    return out


def load_env_file(path: str | Path | None = None, *, force: bool = False
                  ) -> tuple[Path | None, dict[str, str]]:
    """读取凭据文件并把其中的键值写进 os.environ。

    返回 (实际读取到的文件路径, 本次写入的键值对)；文件不存在返回 (None, {})。

    优先级：**外部环境变量 > .env 文件 > config.json 里的 token 字段**。
    同一进程内重复调用时，上次由本文件写入的键会用文件里的新值刷新，
    于是改完 .env 后下一次 load_config 就能生效（无需重启进程）。
    """
    p: Path | None = Path(path) if path else None
    if p is None:
        raw = os.environ.get("DOC2MD_ENV_FILE", "").strip()
        if raw.lower() in _ENV_FILE_OFF:
            return None, {}
        p = Path(raw) if raw else ENV_FILE
    if not p.exists():
        return None, {}
    try:
        text = p.read_text(encoding="utf-8-sig")     # 容忍记事本写出的 BOM
    except UnicodeDecodeError:
        text = p.read_text(encoding="gbk", errors="replace")
    applied: dict[str, str] = {}
    for key, val in _parse_env_text(text).items():
        if not val:
            continue                     # 空值＝占位行，不覆盖任何东西
        if key in os.environ and not force and key not in _ENV_FILE_KEYS:
            continue                     # 外部环境变量优先
        if os.environ.get(key) != val:
            os.environ[key] = val
        _ENV_FILE_KEYS.add(key)
        applied[key] = val
    return p, applied


def _backend_env_key(name: str) -> str:
    """按后端名精确指定 Token 的环境变量名，如 mineru-agent → DOC2MD_TOKEN_MINERU_AGENT。"""
    if not name:
        return ""
    return "DOC2MD_TOKEN_" + "".join(
        c if c.isalnum() else "_" for c in name
    ).upper()


def _backend_env_token(name: str) -> str:
    """按后端名精确指定的 Token（多账号/多后端场景，比厂商级变量更具体）。"""
    key = _backend_env_key(name)
    return os.environ.get(key, "").strip() if key else ""


# VLM 类型下一对多（硅基流动 / 百炼 / 火山 / 魔搭 …），所以厂商级凭据必须先
# 认出厂商才能采用。匹配依据：base_url 的主机名，或后端名本身。
# 记录格式：(厂商标记词, 候选环境变量名[按优先级])
_VLM_VENDOR_ENV: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("siliconflow",), ("DOC2MD_SILICONFLOW_TOKEN", "SILICONFLOW_API_KEY")),
    (("dashscope", "aliyuncs"), ("DOC2MD_DASHSCOPE_TOKEN", "DASHSCOPE_API_KEY")),
    (("volces", "volcengine"), ("DOC2MD_ARK_TOKEN", "ARK_API_KEY")),
    (("modelscope",), ("DOC2MD_MODELSCOPE_TOKEN", "MODELSCOPE_API_KEY")),
)


def _vlm_vendor_keys(b: "OcrConfig") -> tuple[str, ...]:
    """该 VLM 后端所属厂商的候选环境变量名（认不出厂商则返回空）。"""
    host = ""
    try:
        host = (urlparse(b.base_url or "").hostname or "").lower()
    except Exception:
        host = ""
    hay = f"{host} {b.name}".lower()
    for markers, keys in _VLM_VENDOR_ENV:
        if any(m in hay for m in markers):
            return keys
    return ()


def _vlm_vendor_token(b: "OcrConfig") -> str:
    """厂商级 Token。认不出厂商时只认 `DOC2MD_VLM_TOKEN` 这个显式兜底键。

    这样"换个 base_url 就接一家新平台"仍然好用，同时不会把 A 家的 Token
    悄悄发给 B 家（那会变成 401 → 永久熔断）。
    """
    for k in _vlm_vendor_keys(b):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return os.environ.get("DOC2MD_VLM_TOKEN", "").strip()


def mask_token(token: str) -> str:
    """Token 脱敏显示：只留头尾各 4 位，够判断"填没填、填对没填对"。"""
    t = (token or "").strip()
    if not t:
        return "(空)"
    if len(t) <= 12:
        return t[:2] + "*" * (len(t) - 2) if len(t) > 3 else "***"
    return f"{t[:4]}…{t[-4:]}（{len(t)} 位）"


@dataclass
class OcrConfig:
    """**单个云端 OCR 后端**的配置。

    `Config.ocr` 既是"第一个后端"，也是 `backends` 里各条目的默认值来源：
    config.json 的 ocr 段里若写了 `backends` 列表，则每条只写需要覆盖的字段，
    其余继承 ocr 段本身（见 load_config）。这样单后端用户的旧配置一字不改也能跑。
    """

    # ---- 后端标识与路由 ----
    name: str = "paddle"          # 后端唯一名，用于日志、熔断状态、分后端配额统计
    type: str = "paddle"          # paddle | mineru | vlm
    enabled: bool = True
    priority: int = 0             # 越小越先，路由器按此顺序尝试
    # ---- 服务地址与凭据 ----
    base_url: str = "https://paddleocr.aistudio-app.com"
    token: str = ""
    # mineru 专用：是否把 token 放进 Authorization 头。
    #   None（默认）= 自动：precision 接口发（它必须要），agent 接口不发
    #                  —— 轻量接口文档写明「不需要 Authorization」，实测带上
    #                  任何无效 Token 都会 401 A0202，反而把后端打成永久熔断。
    #   True / False = 强制发 / 强制不发（自建代理等特殊场景用）。
    send_token: bool | None = None
    model: str = "PaddleOCR-VL-1.6"
    # mineru 专用：precision（精度接口，需 Token）| agent（轻量接口，免 Token）
    mode: str = ""
    concurrency: int = 4
    poll_interval: float = 4.0
    request_timeout: float = 180.0
    job_timeout: float = 1800.0
    max_retries: int = 5
    # 服务端"提交队列已满"（背压）单独给一套退避参数：
    # 它跟网络故障不同，可能持续几十分钟，必须能扛住，且不能当成文件有问题。
    backpressure_retries: int = 4
    backpressure_base_wait: float = 25.0
    backpressure_max_wait: float = 150.0
    # 连续这么多个文件都撞上背压，就判断服务端整体拥塞，暂停剩余 OCR 留待稍后
    backpressure_abort_streak: int = 3
    daily_page_limit: int = 20000
    # ---- 能力上限：超出即视为"本后端接不了"，交给下一个后端（不熔断）----
    max_pages: int = 0            # 单文件页数上限，0=不限（MinerU 精度接口 200、轻量接口 20）
    max_file_mb: float = 0.0      # 单文件体积上限（MB），0=不限（MinerU 精度 200、轻量 10）
    chunk_over_limit: bool = False  # 页数超 max_pages 时是否自动按 page_ranges 分段提交
    # ---- 熔断开关 ----
    breaker_enabled: bool = True
    breaker_cooldown: float = 300.0   # 背压类故障熔断后的半开试探间隔（秒）
    # ---- 解析选项 ----
    language: str = "ch"          # MinerU 语言包
    is_ocr: bool = True           # MinerU is_ocr
    enable_table: bool = True
    enable_formula: bool = True
    use_doc_orientation_classify: bool = True
    use_doc_unwarping: bool = False
    use_chart_recognition: bool = False
    prettify_markdown: bool = True
    save_ocr_images: bool = True
    # ---- 通用 VLM（OpenAI 兼容 /chat/completions）专用 ----
    # 该后端是否会把插图交还给我们。
    #   true  —— 专用文档解析接口（paddle / mineru precision）会返回图片本体；
    #   false —— 只出文字、不返回图片本体的后端（如 DeepSeek-OCR），md 里不会有图，
    #            引擎据此在首次用到它时提示"这批文件的插图位置会缺失"。
    returns_images: bool = True
    # 送进模型的是什么：
    #   pdf   —— 整段 PDF 直接 base64（SiliconFlow 的 DeepSeek-OCR 支持，由服务端
    #            决定最优分辨率，最省本机算力）
    #   image —— 本机用 pymupdf 逐页渲染成 PNG 再 base64（换到不支持 PDF 的 VLM 时用）
    # 留空则由客户端按 type 取默认（vlm → pdf）。
    input_mode: str = ""
    # 提示词。DeepSeek-OCR 认这几套（见 SiliconFlow 官方文档）：
    #   "<image>\n<|grounding|>Convert the document to markdown."  文档转 Markdown
    #   "<image>\n<|grounding|>OCR this image."                    通用 OCR
    #   "<image>\nFree OCR."                                       无布局提取
    # 留空则按 input_mode 取上面第一套。
    prompt: str = ""
    temperature: float = 0.0      # 转录类任务要确定性，默认 0（别用平台的 0.7 默认值）
    max_tokens: int = 8192        # 不少平台不传会直接报错；单次请求的输出上限
    image_detail: str = "high"    # auto | low | high（影响视觉 token 数与清晰度）
    dpi: int = 200                # input_mode=image 时的光栅化分辨率
    # image 模式下每页单独发一次请求（true）还是把整段的几页作为多图一次发（false）。
    # 专用 OCR 模型建议 true：多图同时送入时它对额外页面的注意力会明显下降。
    one_page_per_request: bool = True
    # 由 load_config 填充：本后端生效的配置列表（仅 Config.ocr 上非空）
    backends: list["OcrConfig"] = field(default_factory=list)


@dataclass
class LocalOcrConfig:
    """本地 RapidOCR（PP-OCRv6）后端配置。

    与云端 OCR 的关系：默认扫描件先走本地（快、免费、合规），
    检测到复杂表格时自动转发云端兜底（见 ocr_complex_fallback）。
    """

    enabled: bool = True
    # 独立的 RapidOCR 解释器路径。**留空即自动禁用本地 OCR**（见 load_config），
    # 全部扫描件走云端链路 —— 云端链路整体不可用时会把文件标 deferred 待重跑，
    # 不会以低质量结果记账。要用本地 OCR 就填绝对路径，例如
    #   C:\Tools\PaddleOCR\.venv\Scripts\python.exe
    python_exe: str = ""
    device: str = "dml"          # dml | cpu
    dpi: int = 200               # PDF 渲染分辨率
    concurrency: int = 1         # 本地子进程串行，通常保持 1
    # 是否优先走云端（PaddleOCR-VL）。
    #   false（默认）：本地 RapidOCR 先跑，只有复杂表才转云端——免费、不上传，但慢
    #                  （实测约 2 s/页），且大面积表格靠启发式还原，保真度一般。
    #   true：直接走云端 VLM（实测约 1.2 s/页/任务，并发下更快），表格/版式保真更好，
    #         但会把文档上传到 PaddleOCR 服务；敏感目录仍按 shield_sensitive_for_ocr
    #         强制留本地，绝不上云。
    prefer_cloud: bool = False
    # 云端整体不可用（配额用尽 / 持续背压）时是否降级本地识别。
    #   false（默认）：不降级，把文件标 deferred 留待重跑 —— 避免这些文件以
    #                  较低质量被记成 ok，从此再也不会用云端重做。
    #   true：直接用本地 RapidOCR 出结果（快、免费），但质量与云端 VLM 有差距。
    local_fallback_on_cloud_down: bool = False
    # 复杂表格检测阈值：列数超过此值视为复杂，转发云端
    complex_col_threshold: int = 12
    # 是否允许复杂表自动转发云端（false 则复杂表也强制本地启发式输出）
    ocr_complex_fallback: bool = True


@dataclass
class OutputConfig:
    """Markdown 输出位置。

    alongside —— 与原文件同目录、同名（默认）
    custom    —— 统一存到 root 下，配合 layout 决定目录结构
    """

    mode: str = "alongside"   # alongside | custom
    root: str = ""            # custom 模式的目标根目录（绝对路径）
    layout: str = "mirror"    # custom 模式下：mirror 保留原目录结构 / flat 全部平铺
    # 两个不同源文件算出同一个 md 路径时怎么办（如同一目录下的 x.docx 与 x.pdf 都指向 x.md）：
    #   stable    —— 先到先得 + 归属粘住（默认）。已在状态库里归属过的路径永久保留，
    #                后到者另存 x_<hash>.md；hash 由源文件路径决定，所以**重转永远同名**，
    #                只会覆盖自己那一份，不会再多冒出第三个文件。
    #   overwrite —— 同名直接覆盖，一个名字只剩一个文件。注意：同名的两个源若是
    #                同一文档的不同格式（docx 与 pdf），会丢掉其中一份的内容。
    #   suffix    —— 旧行为：没有粘性，每次重转重新抢一次，抢不到就加 hash（名字会漂移）。
    on_collision: str = "stable"

    @property
    def is_custom(self) -> bool:
        return self.mode == "custom" and bool(self.root.strip())


@dataclass
class Config:
    roots: list[str] = field(default_factory=list)
    exclude_dir_names: list[str] = field(default_factory=list)
    sensitive_markers: list[str] = field(default_factory=list)
    watch_extensions: list[str] = field(default_factory=list)
    image_extensions: list[str] = field(default_factory=list)
    keep_original: bool = True
    overwrite_existing_md: str = "skip"
    output: OutputConfig = field(default_factory=OutputConfig)
    min_text_chars_per_page: int = 80
    text_pdf_probe_pages: int = 5
    # 文本层可信度检查：字数够但实为"扫描件 + OCR 文本层 / CID 乱码 / 隐形层"的
    # PDF 改走 OCR 重新识别（扫描件自带的 OCR 文本层不可靠，见 detect.pdf_text_trust）
    pdf_trust_check: bool = True
    shield_sensitive_for_ocr: bool = True
    # pymupdf4llm 的 ONNX 版面模型：会拉起子进程，且在 GBK 环境下有已知
    # 编码 bug（stderr 刷 UnicodeDecodeError）并慢十几倍。默认关闭。
    pdf_use_layout: bool = False
    max_excel_rows: int = 2000
    max_excel_cols: int = 60
    excel_sheet_limit: int = 20
    debounce_seconds: float = 3.0
    state_db: Path = TOOL_DIR / "state.db"
    log_dir: Path = TOOL_DIR / "logs"
    ocr: OcrConfig = field(default_factory=OcrConfig)
    local_ocr: LocalOcrConfig = field(default_factory=LocalOcrConfig)
    # 实际读取到的凭据文件（.env）与其中生效的键名（只记键名，不留 Token 明文）
    env_file: Path | None = None
    env_keys: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def watch_ext_set(self) -> set[str]:
        return {e.lower() for e in self.watch_extensions}

    @property
    def image_ext_set(self) -> set[str]:
        return {e.lower() for e in self.image_extensions}

    @property
    def ocr_backends(self) -> list[OcrConfig]:
        """生效的云端 OCR 后端列表（按尝试优先级）。

        配了 `ocr.backends` 就用那份列表；没配则把 `ocr` 段自身当作唯一后端，
        于是"单后端"这条老路径的行为与改造前完全一致。
        """
        return list(self.ocr.backends) if self.ocr.backends else [self.ocr]


def _token_from_mcp_json() -> str:
    """从 WorkBuddy 的 mcp.json 里兜底读取 PaddleOCR Token。"""
    candidates = [
        Path.home() / ".workbuddy" / "mcp.json",
        Path.home() / ".workbuddy" / ".mcp.json",
    ]
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        servers = data.get("mcpServers", {})
        for name, cfg in servers.items():
            if "paddleocr" in name.lower():
                env = cfg.get("env", {})
                for k, v in env.items():
                    if "TOKEN" in k.upper() and v:
                        return str(v)
    return ""


def _apply_backend_env(ocr: OcrConfig) -> None:
    """用环境变量（含 .env 凭据文件）补/覆盖各后端的 Token。

    单个后端的 Token 取值优先级（高 → 低）：
      1. `DOC2MD_TOKEN_<后端名>`        按后端精确指定，如 DOC2MD_TOKEN_MINERU_AGENT
      2. 厂商级变量（该厂商所有后端通用）：
           mineru → DOC2MD_MINERU_TOKEN / MINERU_TOKEN
           vlm    → 见 _VLM_VENDOR_ENV，按 base_url 主机名或后端名匹配厂商
      3. config.json 里该后端自己的 `token` 字段
    `DOC2MD_MINERU_BASE_URL` 覆盖 MinerU 服务地址，
    `DOC2MD_VLM_BASE_URL` 覆盖 VLM 服务地址（换成别的 OpenAI 兼容平台时用）。

    vlm 类型要特别小心：同一个 type 底下可能是硅基流动、百炼、火山……所以
    厂商级变量必须**先匹配厂商**再采用，否则 DOC2MD_SILICONFLOW_TOKEN 会被灌进
    别家的后端 → 401 → 鉴权类故障 → 永久熔断。这与当初 mineru-agent 误继承
    PaddleOCR Token 是同一类坑，只是这次跨的是厂商而不是跨类型。

    注意：`.env` 里的值在 load_config 时已经被写进 os.environ，所以这里只需要
    看 os.environ，不必区分"来自文件"还是"来自真实环境变量"。
    """
    mineru_token = (
        os.environ.get("DOC2MD_MINERU_TOKEN")
        or os.environ.get("MINERU_TOKEN")
        or ""
    ).strip()
    mineru_base = os.environ.get("DOC2MD_MINERU_BASE_URL", "").strip()
    vlm_base = os.environ.get("DOC2MD_VLM_BASE_URL", "").strip()
    for b in ocr.backends or []:
        specific = _backend_env_token(b.name)
        if specific:
            b.token = specific
        elif b.type == "mineru" and mineru_token:
            b.token = mineru_token
        elif b.type == "vlm":
            vendor = _vlm_vendor_token(b)
            if vendor:
                b.token = vendor
        if b.type == "mineru" and mineru_base:
            b.base_url = mineru_base
        elif b.type == "vlm" and vlm_base:
            b.base_url = vlm_base


def resolve_config_path(path: str | Path | None = None) -> Path:
    """把配置路径规范化，缺省时用内置默认路径。"""
    return Path(path) if path else DEFAULT_CONFIG


def bootstrap_config(path: str | Path | None = None) -> Path | None:
    """首次运行时用同目录的 config.example.json 生成 config.json。

    仓库里**只提交示例配置**（不含任何个人目录与凭据），真实的 config.json /
    .env / state.db 一律在 .gitignore 里。这样 clone 下来不缺配置、双击 bat 就能
    跑，也不会把别人的路径与 Token 带进版本库。

    返回实际生成的文件路径；没有生成则返回 None。
    """
    p = resolve_config_path(path)
    if p.exists() or p.name != "config.json":
        return None
    example = p.with_name("config.example.json")
    if not example.exists():
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, p)
    except OSError:
        return None
    return p


def load_config(path: str | Path | None = None) -> Config:
    p = resolve_config_path(path)
    if not p.exists():
        bootstrap_config(p)
    if not p.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {p}\n"
            f"  把同目录的 config.example.json 复制成 config.json 再按需修改即可。"
        )

    # 凭据文件：优先与 config.json 同目录，其次 DOC2MD_ENV_FILE 指定的路径 / 工具目录 .env。
    # 全部 Token 都在这里维护，config.json 里不再写凭据。
    env_file, env_applied = load_env_file(Path(p).parent / ".env")
    if env_file is None and Path(p).parent != TOOL_DIR:
        env_file, env_applied = load_env_file()

    data = json.loads(p.read_text(encoding="utf-8"))

    ocr_raw = dict(data.get("ocr", {}))
    # PaddleOCR Token 取值链：DOC2MD_PADDLE_TOKEN（.env 里更好记的名字）
    #   → PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN（官方 MCP 用的名字，兼容旧配置）
    #   → config.json 的 ocr.token（旧配置兜底）
    #   → WorkBuddy mcp.json（更老的兜底）
    token = (
        os.environ.get("DOC2MD_PADDLE_TOKEN")
        or os.environ.get("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN")
        or ocr_raw.get("token")
        or _token_from_mcp_json()
    )
    ocr_raw["token"] = (token or "").strip()

    known_ocr = {f for f in OcrConfig.__dataclass_fields__ if f != "backends"}
    base_kwargs = {k: v for k, v in ocr_raw.items() if k in known_ocr}
    base_type = str(base_kwargs.get("type") or "paddle")
    # 多后端：ocr 段的 backends 列表逐条覆盖 ocr 段本身的字段取值。
    # 没写 backends 就退化成"单后端"，与旧配置完全等价。
    entries = ocr_raw.get("backends") or []
    if entries:
        occ_list: list[OcrConfig] = []
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            kw = {k: v for k, v in e.items() if k in known_ocr}
            kw.setdefault("priority", i)
            btype = str(kw.get("type") or base_type)
            if btype != base_type:
                # 跨厂商时**不继承**凭据与服务地址。
                # 曾踩过：ocr 段是 PaddleOCR（token 非空），mineru-agent 那一条没写
                # token，于是继承了 Paddle 的 token 并发给 MinerU →
                # 401 A0202 user authenticate failed → 该后端被永久熔断，
                # 备用通道形同虚设（冒烟测试正是这样全军覆没的）。
                kw.setdefault("token", "")
                if btype in DEFAULT_BASE_URL:
                    kw.setdefault("base_url", DEFAULT_BASE_URL[btype])
            occ_list.append(_dc_replace(
                OcrConfig(**base_kwargs), backends=[], **kw
            ))
        # 稳定排序：priority 相同则保留配置文件里的先后顺序
        occ_list.sort(key=lambda b: b.priority)
    else:
        occ_list = []
    ocr = OcrConfig(**base_kwargs)
    ocr.backends = occ_list
    _apply_backend_env(ocr)

    local_raw = dict(data.get("local_ocr", {}))
    known_local = {f for f in LocalOcrConfig.__dataclass_fields__}
    local_ocr = LocalOcrConfig(**{k: v for k, v in local_raw.items() if k in known_local})
    if local_ocr.enabled and not str(local_ocr.python_exe or "").strip():
        # 没配本地 RapidOCR 解释器 → 直接禁用。否则每碰到一个扫描件就刷一句
        # "本地 OCR 不可用（RapidOCR Python 不存在：）"，噪音大且没有信息量。
        local_ocr.enabled = False

    out_raw = dict(data.get("output", {}))
    known_out = {f for f in OutputConfig.__dataclass_fields__}
    output = OutputConfig(**{k: v for k, v in out_raw.items() if k in known_out})
    if output.mode not in ("alongside", "custom"):
        output.mode = "alongside"
    if output.layout not in ("mirror", "flat"):
        output.layout = "mirror"
    if output.on_collision not in ("overwrite", "stable", "suffix"):
        output.on_collision = "stable"
    if output.mode == "custom" and not output.root.strip():
        # 配了 custom 但没给目录，退回默认行为，避免把 md 写到奇怪的地方
        output.mode = "alongside"

    cfg = Config(
        roots=data.get("roots", []),
        exclude_dir_names=data.get("exclude_dir_names", []),
        sensitive_markers=data.get("sensitive_markers", []),
        watch_extensions=data.get("watch_extensions", []),
        image_extensions=data.get("image_extensions", []),
        keep_original=data.get("keep_original", True),
        overwrite_existing_md=data.get("overwrite_existing_md", "skip"),
        output=output,
        min_text_chars_per_page=data.get("min_text_chars_per_page", 80),
        text_pdf_probe_pages=data.get("text_pdf_probe_pages", 5),
        pdf_trust_check=data.get("pdf_trust_check", True),
        shield_sensitive_for_ocr=data.get("shield_sensitive_for_ocr", True),
        pdf_use_layout=data.get("pdf_use_layout", False),
        max_excel_rows=data.get("max_excel_rows", 2000),
        max_excel_cols=data.get("max_excel_cols", 60),
        excel_sheet_limit=data.get("excel_sheet_limit", 20),
        debounce_seconds=data.get("debounce_seconds", 3.0),
        ocr=ocr,
        local_ocr=local_ocr,
        env_file=env_file,
        env_keys=sorted(env_applied),
        raw=data,
    )

    if data.get("state_db"):
        cfg.state_db = Path(data["state_db"])
    if data.get("log_dir"):
        cfg.log_dir = Path(data["log_dir"])
    if not cfg.roots:
        # 没配处理目录时退化成"工具自己的仓库根"（示例配置就是这个状态）。
        # 启动横幅会把「处理目录」打出来，看到不对就去改 config.json 的 roots。
        cfg.roots = [str(TOOL_DIR)]

    return cfg


def is_sensitive(cfg: Config, path: str | Path) -> bool:
    """路径是否命中敏感标记（用于阻断云端 OCR）。"""
    if not cfg.shield_sensitive_for_ocr:
        return False
    parts = [x.lower() for x in Path(path).parts]
    for m in cfg.sensitive_markers:
        key = m.lower()
        if any(key in part for part in parts):
            return True
    return False


def is_excluded(cfg: Config, path: str | Path) -> bool:
    """路径是否位于排除目录内。"""
    parts = [x.lower() for x in Path(path).parts]
    for name in cfg.exclude_dir_names:
        if name.lower() in parts:
            return True
    return False


def is_in_output_root(cfg: Config, path: str | Path) -> bool:
    """路径是否落在 md 输出目录内（避免监控自己产出的目录）。"""
    if not cfg.output.is_custom:
        return False
    try:
        Path(path).resolve().relative_to(Path(cfg.output.root).resolve())
        return True
    except (ValueError, OSError):
        return False


def describe_output(cfg: Config) -> str:
    """给界面用的一句话描述输出位置。"""
    o = cfg.output
    if not o.is_custom:
        return "与原文件同目录、同名（alongside）"
    layout = "保留原目录结构" if o.layout == "mirror" else "全部平铺"
    coll = {
        "stable": "同名先到先得、重转不改名",
        "overwrite": "同名直接覆盖",
        "suffix": "同名加哈希后缀",
    }.get(o.on_collision, o.on_collision)
    return f"{o.root}（custom / {layout} / {coll}）"


TYPE_LABELS = {
    "paddle": "PaddleOCR",
    "mineru": "MinerU",
    "vlm": "VLM",
}


def backend_label(b: OcrConfig) -> str:
    """后端的显示标签，如 `mineru[MinerU/precision]`、`sf-ocr[VLM:DeepSeek-OCR·无插图]`。

    把"不返回插图"直接标在标签上，是因为这一点最容易在事后被忽略：
    从 md 上看不出插图是"本来就没有"还是"后端没给"。
    """
    tag = TYPE_LABELS.get(b.type, b.type or "?")
    if b.type == "mineru" and b.mode:
        tag += f"/{b.mode}"
    if b.type == "vlm" and b.model:
        tag += ":" + str(b.model).split("/")[-1]
    if not b.returns_images:
        tag += "·无插图"
    return f"{b.name}[{tag}]"


def describe_backends(cfg: Config) -> str:
    """给界面用的一句话描述云端 OCR 后端链路（按熔断切换顺序）。"""
    names = []
    for b in cfg.ocr_backends:
        label = backend_label(b)
        names.append(label if b.enabled else f"{label}（已禁用）")
    return " → ".join(names) if names else "（未配置）"


def credential_source(b: OcrConfig) -> str:
    """某个后端的 Token 是**从哪儿读到的**（只报来源键名，不泄露 Token 内容）。

    依据 _apply_backend_env 的优先级反推：环境变量（含 .env）里有值就是它，
    否则才是 config.json 里写的。
    """
    key = _backend_env_key(b.name)
    if key and os.environ.get(key, "").strip():
        return f"env/.env · {key}"
    if b.type == "mineru":
        for k in ("DOC2MD_MINERU_TOKEN", "MINERU_TOKEN"):
            if os.environ.get(k, "").strip():
                return f"env/.env · {k}"
    if b.type == "paddle":
        for k in ("DOC2MD_PADDLE_TOKEN", "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN"):
            if os.environ.get(k, "").strip():
                return f"env/.env · {k}"
    if b.type == "vlm":
        for k in (*_vlm_vendor_keys(b), "DOC2MD_VLM_TOKEN"):
            if os.environ.get(k, "").strip():
                return f"env/.env · {k}"
    if b.token:
        return "config.json"
    return "未配置"


def describe_credentials(cfg: Config) -> list[str]:
    """给界面用：逐后端说明凭据状态（脱敏），一眼能看出哪个后端为什么没进链路。"""
    lines: list[str] = []
    for b in cfg.ocr_backends:
        tag = backend_label(b)
        if b.type == "mineru" and b.mode == "agent" and b.send_token is not True:
            note = ("轻量接口免鉴权，不发送 Authorization"
                    + ("（已备 Token，切 precision 时用）" if b.token else ""))
        elif b.token:
            note = f"{mask_token(b.token)}　← {credential_source(b)}"
        elif b.type == "mineru" and b.mode == "agent":
            note = "免 Token 即可用"
        else:
            note = "[未配置] Token 为空，该后端不会纳入链路"
        if b.type == "vlm" and b.model:
            note += f"　· 模型 {b.model}"
        lines.append(f"{tag}　{note}")
    return lines


def describe_env_file(cfg: Config) -> str:
    """给界面用的一句话描述凭据文件状态。"""
    if cfg.env_file is None:
        return f"未找到 .env（期望位置：{ENV_FILE}）—— Token 只能从 config.json 或系统环境变量读"
    n = len(cfg.env_keys)
    keys = "、".join(cfg.env_keys) if cfg.env_keys else "（没有非空的键）"
    return f"{cfg.env_file}（生效 {n} 项：{keys}）"
