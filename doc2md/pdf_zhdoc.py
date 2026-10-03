"""接入 vendor/ZhDocParser 的 PdfExtractor —— 纯规则、零神经网络的 PDF → Markdown。

为什么把上游 vendor 进来，而不是照它重写一遍
--------------------------------------------
1. 上游在持续更新（`git subtree pull` 一条命令即可同步）；
2. 我们的修正可以原样 PR 回去，上游采纳后 pull 回来即自动生效；
3. 照着实现重写等于分叉成两份，之后必然各自漂移、互相打脸。

改动策略：**上游只管通用能力**。doc2md 特有的输出约定（渲染层级、表格原位、
段落规则与现有链路保持一致）一律放在本文件做，不去动上游的核心逻辑 ——
这样上游改动面最小，`subtree pull` 才不容易冲突。

上游已做的修正（见 vendor/ZhDocParser，每条都可单独 PR 回去）
-------------------------------------------------------------
1. `zhdocparser/__init__.py` 惰性导入 —— 否则任何子模块导入都会连带拉进
   sdk → service → factory → docx_extractor → python-docx/lxml；
2. `import fitz` → `import pymupdf as fitz` —— PyMuPDF 1.28 起 `fitz` 会打印
   "The `fitz` API is deprecated…" 警告，未来版本将移除；
3. 编号正则顺序 —— 原顺序里 `^\\d+[、.]` 排在 `^\\d+\\.\\d+\\.\\d+` 之前，
   `1.1.1 xxx` 会被前缀 `1.` 抢先匹配而降级成二级；
4. **旋转页面坐标归一** —— `get_text()` 返回未旋转坐标，而 `page.rect` 是旋转后
   的显示尺寸；rotation != 0 的 PDF（WPS 导出的红头文件就是）两者不同尺度，
   会让「整行宽度 >= 页宽 × 0.65」等判断全线错位，把单栏正文误判成多栏、
   重排出错误阅读顺序（正文被排到标题前面）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

__all__ = ["pdf_to_markdown_zhdoc", "vendor_dir"]

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "ZhDocParser"


def vendor_dir() -> Path:
    """vendor 子项目的绝对路径（开发态与打包态都是这一个）。"""
    return _VENDOR_DIR


# ---------------- 版头元信息：醒目字号，但不是章节标题 ----------------
_MASTHEAD = (
    # 发文字号：`办〔2022〕-10`、`遂船府办发〔2023〕15号`
    re.compile(r"^[\u4e00-\u9fa5]{0,12}〔\d{4}〕\s*[-—－]?\s*\d*\s*号?$"),
    # 成文日期（中文写法）
    re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$"),
    # 标准/规程封面上的发布-实施日期（`2005-04-13发布`）
    re.compile(r"^\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}\s*(?:发布|实施|印发|修订|批准)?$"),
    # 标准分类号
    re.compile(r"^(?:ICS|CCS)\s*[\d.]+\s*$"),
    # 孤零零的"附件"/"附录"标签
    re.compile(r"^(附\s*件|附\s*录)\s*[:：]?$"),
)

# 整行只有一个编号、没有实质文字：`3.2`、`一、`、`（1）`。
# 中文标准（GB/AQ/HG 那类）的文本层常把 `3.2` 与它后面的标题**拆成两行** ——
# 不合并的话会得到一串 `### 3.2` / `### 3.3` 的空标题，标题数虚高好几倍。
_BARE_NUMBER = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[条章节]"
    r"|[一二三四五六七八九十]+\s*[、.]"
    r"|[（(]\s*[一二三四五六七八九十0-9]+\s*[）)]"
    r"|\d+(?:\.\d+)*\s*[、.]?)$"
)

# 主送机关：`各乡镇党委，各街道、园区党工委，各乡镇人民政府、街道办事处：`
_ADDRESSEE = re.compile(r"^[^。；！？]{4,80}[：:]$")

# 标题行不该以这些收尾（后面还有下半句）
_HEADING_TERMINALS = ("。", "；", "！", "？", "，", "、", "：")

# 发文机关名：`中共遂宁市船山区委办公室`。这类行字号常与标题**完全一致**，
# 光靠字号分不开，只能按"机关名后缀"识别 —— 公文标题一律以文种收尾
# （通知/意见/方案/报告…），不会以"办公室/人民政府"结尾。
_ORG_TAIL = re.compile(
    r"(办公室|办公厅|委员会|人民政府|党委|党组|人大常委会|政协|纪委|监委"
    r"|法院|检察院|工会|团委|妇联|管理局|局|厅|部|委|办|政府|人大)$"
)

# 跨行标题合并允许的字号差：同一标题的两半字号几乎一致
_MERGE_SIZE_TOL = 1.0

# 标题与正文挤在同一行：`（一）整合职能组建监管执法机构。将法律法规赋予县级应…`
_SPLIT_HEAD_BODY = re.compile(
    r"^((?:第[一二三四五六七八九十百]+[条章节]"
    r"|[一二三四五六七八九十]+\s*、"
    r"|[（(]\s*[一二三四五六七八九十0-9]+\s*[）)]"
    r"|\d+\.\d+(?:\.\d+)*"
    r"|\d+\s*[、.](?!\d))"
    r"\s*[^。；！？]{0,26})[。；]\s*(\S.{3,})$"
)

_MAX_MERGE_CHARS = 40          # 跨行标题合并时，任一半的长度上限
_MAX_HEAD_CHARS = 40           # 切分出来的标题部分长度上限


def _norm(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


# ---------------- 加载 vendor ----------------

_extractor = None
_path_ready = False


def _ensure_vendor_path() -> None:
    """保证 vendor 目录在 sys.path 上。

    开发态直接跑 .py 时需要；打包态 spec 的 pathex 已把 zhdocparser 编进 PYZ，
    第一句 import 就成功、直接返回。
    """
    global _path_ready
    if _path_ready:
        return
    try:
        import zhdocparser  # noqa: F401
    except ImportError:
        if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
            sys.path.insert(0, str(_VENDOR_DIR))
    _path_ready = True


def _get_extractor():
    global _extractor
    if _extractor is None:
        _ensure_vendor_path()
        from zhdocparser.extractors.pdf_extractor import PdfExtractor

        _extractor = PdfExtractor()
    return _extractor


def _pattern_level(text: str) -> int:
    """借用上游的编号→层级判断（静态方法，不算改上游）。"""
    _ensure_vendor_path()
    from zhdocparser.extractors.pdf_extractor import PdfExtractor

    return PdfExtractor._heading_pattern_level(text)


# ---------------- 章节规范化：合并 / 切分 / 降级 ----------------

def _normalize_sections(document) -> list[tuple[int, str, list[str], int, int, float]]:
    """把上游的 sections 整理成 (层级, 标题, 正文行, 起始页, 结束页, 字号)。

    做三件上游没做的事：
      * 跨行标题合并（`…综合行政执法改革` + `改革实施方案》的通知`）
      * 标题与正文挤在同一行时切开
      * 版头元信息（文号 / 成文日期 / 主送机关）从标题降级为正文
    """
    items: list[tuple[int, str, list[str], int, int, float]] = []

    for sec in document.sections:
        heading = _norm(sec.heading)
        level = sec.level or 1
        lines = [ln for ln in (sec.content or "").split("\n") if ln.strip()]
        page_lo = sec.page_number or 1
        page_hi = sec.page_end or page_lo
        size = float((sec.metadata or {}).get("font_size") or 0.0)

        # (a) 标题与正文同行 → 切开
        m = _SPLIT_HEAD_BODY.match(heading)
        if m and len(m.group(1)) <= _MAX_HEAD_CHARS:
            heading = m.group(1).strip()
            lines.insert(0, m.group(2).strip())

        # (b) 版头元信息 → 降级为正文
        if heading and (
            any(rx.match(heading) for rx in _MASTHEAD)
            or _ADDRESSEE.match(heading)
        ):
            lines.insert(0, heading)
            heading, level = "", 0

        # (c) 纯编号行 + 紧邻的标题文本 → 合并回去
        #     （中文标准的文本层常把 `3.2` 和它的标题拆成两行）
        if (
            heading
            and items
            and items[-1][1]
            and _BARE_NUMBER.match(items[-1][1])
            and not items[-1][2]
            and len(heading) <= _MAX_MERGE_CHARS
        ):
            prev = items[-1]
            items[-1] = (prev[0], f"{prev[1]} {heading}", prev[2], prev[3],
                         max(prev[4], page_hi), prev[5])
            continue

        # (d) 跨行标题合并：前一个标题还没吐正文，且两半都不像完整句子
        if (
            heading
            and items
            and items[-1][1]
            and not items[-1][2]                      # 上一个标题后面还没正文
            and items[-1][0] == level
            and not items[-1][1].endswith(_HEADING_TERMINALS)
            and not _ORG_TAIL.search(items[-1][1])    # 别把发文机关粘到标题上
            and abs(items[-1][5] - size) <= _MERGE_SIZE_TOL
            and len(items[-1][1]) <= _MAX_MERGE_CHARS
            and len(heading) <= _MAX_MERGE_CHARS
            and _pattern_level(heading) == 0          # 下半句不该自带编号
        ):
            prev = items[-1]
            items[-1] = (prev[0], prev[1] + heading, prev[2], prev[3], max(prev[4], page_hi), prev[5])
            continue

        items.append((level, heading, lines, page_lo, page_hi, size))

    return items


# ---------------- 渲染 ----------------

def _render_level(level: int, heading: str) -> int:
    """编号型标题整体下沉一级，把 h1 让给文档大标题。

    公文/国标的大标题靠**字号**取胜（level 1），而 `一、`/`（一）`/`1.1` 这些
    是靠**编号**（level 1/2/3）。两者混在同一层会让 Markdown 目录全平。
    编号型 +1 之后：大标题 h1、`一、` h2、`（一）` h3 —— 层级对上了。
    """
    if level <= 0:
        return 0
    if _pattern_level(heading) > 0:
        level += 1
    return max(1, min(level, 6))


def _render(document, *, max_cols: int) -> str:
    from .converters import _rows_to_table

    items = _normalize_sections(document)
    tables = list(document.tables)
    out: list[str] = []

    def flush_tables(anchor: str) -> None:
        for t in list(tables):
            ctx = t.context_before or []
            if not ctx:
                continue
            if _norm(ctx[-1]) != anchor:
                continue
            md = _rows_to_table(t.rows, max_cols)
            if md:
                out.append("")
                out.append(md)
            tables.remove(t)

    for level, heading, lines, _lo, _hi, _fs in items:
        if heading:
            out.append("#" * _render_level(level, heading) + " " + heading)
            out.append("")
        for ln in lines:
            out.append(ln)
            flush_tables(ln)
        if lines:
            out.append("")

    # 锚点没对上的表格（正文里找不到那一行）→ 按页序补在文末，总比丢掉强
    for t in tables:
        md = _rows_to_table(t.rows, max_cols)
        if md:
            out.append("")
            out.append(md)

    return "\n".join(out)


def pdf_to_markdown_zhdoc(src: Path, *, max_cols: int = 60) -> str:
    """文本型 PDF → Markdown，走 vendor 的纯规则提取器（零模型）。"""
    from .converters import cleanup_markdown, merge_pdf_lines
    from .detect import silence_mupdf

    silence_mupdf()
    document = _get_extractor().extract(Path(src))
    raw = _render(document, max_cols=max_cols)
    # 上游的 section.content 是"一视觉行一段"，与 layout 档一样，
    # 统一交给 merge_pdf_lines 按中文行文规则重组自然段。
    return cleanup_markdown(merge_pdf_lines(raw))
