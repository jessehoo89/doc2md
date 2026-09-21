"""本地转换器：docx / xlsx / xls / 文本型 PDF / HTML → Markdown，全部离线、零成本。"""
from __future__ import annotations

import html as html_mod
import itertools
import mimetypes
import re
from html.parser import HTMLParser
from pathlib import Path

# ---------------- Markdown 收尾清理 ----------------

_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAIL_SPACE = re.compile(r"[ \t]+\n")


def cleanup_markdown(text: str) -> str:
    """统一换行、压掉多余空行与行尾空白。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")          # 全角空格
    text = _TRAIL_SPACE.sub("\n", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    lines = [ln for ln in text.split("\n")]
    # 去掉首尾空行
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------- Word ----------------

def _image_handler(assets_dir: Path, rel_prefix: str):
    """mammoth 的图片回调：把内嵌图片落盘到同名 .assets 目录。"""
    counter = itertools.count(1)

    def handler(image):
        ext = mimetypes.guess_extension(image.content_type or "") or ".png"
        if ext == ".jpe":
            ext = ".jpg"
        name = f"image_{next(counter):03d}{ext}"
        try:
            assets_dir.mkdir(parents=True, exist_ok=True)
            with image.open() as f:
                (assets_dir / name).write_bytes(f.read())
            return {"src": f"{rel_prefix}/{name}"}
        except Exception:
            return {}

    return handler


def docx_to_markdown(src: Path, assets_dir: Path | None = None) -> str:
    """docx → Markdown。优先保留标题层级、列表、表格与图片。

    assets_dir 为图片落盘目录；rel_prefix 是 Markdown 里引用的相对路径前缀。
    """
    import mammoth
    import markdownify

    kwargs = {}
    if assets_dir is not None:
        kwargs["convert_image"] = mammoth.images.img_element(
            _image_handler(assets_dir, assets_dir.name)
        )
    with open(src, "rb") as f:
        result = mammoth.convert_to_html(f, **kwargs)

    md = markdownify.markdownify(
        result.value,
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
    )
    return cleanup_markdown(md)


# ---------------- Excel ----------------

_PIPE = re.compile(r"\|")


def _cell_to_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    s = s.replace("\n", " ").replace("\r", " ")
    s = _PIPE.sub("\\|", s)
    return s


def _rows_to_table(rows: list[list], max_cols: int) -> str:
    """二维数据 → GFM 表格。自动裁掉右侧全空列。"""
    # 裁列
    width = 0
    for r in rows:
        for i, v in enumerate(r):
            if _cell_to_text(v):
                width = max(width, i + 1)
    width = min(width, max_cols)
    if width == 0:
        return ""
    out = []
    header = [_cell_to_text(v) for v in rows[0][:width]]
    if not any(header):
        header = [f"列{i + 1}" for i in range(width)]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "---|" * width)
    for r in rows[1:]:
        cells = [_cell_to_text(v) for v in list(r)[:width]]
        cells += [""] * (width - len(cells))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _is_zip_container(p: Path) -> bool:
    """文件头是不是 zip（即 OOXML 容器）。"""
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _load_xlsx_tolerant(src: Path):
    """打开工作簿；容忍「内容其实是 OOXML、扩展名却写成 .xls」的错名文件。

    openpyxl 只按扩展名判断，见到 .xls 直接抛 InvalidFileException 让人改格式。
    但这种文件实际是 zip 容器，能正常读。这里按魔数复核后复制成 .xlsx 临时文件再读。
    返回 (workbook, 临时文件或 None)。read_only 是按需读取的，临时文件必须等
    wb.close() 之后才能删。
    """
    import os
    import shutil
    import tempfile

    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        return openpyxl.load_workbook(str(src), read_only=True, data_only=True), None
    except InvalidFileException:
        if not _is_zip_container(src):
            raise
        fd, name = tempfile.mkstemp(prefix="doc2md_asxlsx_", suffix=".xlsx")
        os.close(fd)
        tmp = Path(name)
        shutil.copy2(src, tmp)
        try:
            return openpyxl.load_workbook(str(tmp), read_only=True, data_only=True), tmp
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def xlsx_to_markdown(
    src: Path, max_rows: int = 2000, max_cols: int = 60, sheet_limit: int = 20
) -> str:
    """xlsx → Markdown，每个工作表一节。"""
    wb, tmp = _load_xlsx_tolerant(src)
    parts: list[str] = []
    try:
        sheets = wb.sheetnames[:sheet_limit]
        if len(wb.sheetnames) > sheet_limit:
            parts.append(f"> 工作表过多，仅转换前 {sheet_limit} 个，共 {len(wb.sheetnames)} 个。\n")
        for name in sheets:
            ws = wb[name]
            rows = []
            truncated = False
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    truncated = True
                    break
                rows.append(list(row))
            parts.append(f"## {name}\n")
            if not rows:
                parts.append("_（空工作表）_\n")
                continue
            table = _rows_to_table(rows, max_cols)
            parts.append(table + "\n")
            if truncated:
                parts.append(f"> 数据超过 {max_rows} 行，已截断。\n")
    finally:
        wb.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return cleanup_markdown("\n".join(parts))


def xls_to_markdown(
    src: Path, max_rows: int = 2000, max_cols: int = 60, sheet_limit: int = 20
) -> str:
    """老式 .xls → Markdown。"""
    import xlrd

    book = xlrd.open_workbook(str(src))
    parts: list[str] = []
    names = book.sheet_names()[:sheet_limit]
    if book.nsheets > sheet_limit:
        parts.append(f"> 工作表过多，仅转换前 {sheet_limit} 个，共 {book.nsheets} 个。\n")
    for name in names:
        ws = book.sheet_by_name(name)
        nrows = min(ws.nrows, max_rows)
        rows = [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(nrows)]
        parts.append(f"## {name}\n")
        if not rows:
            parts.append("_（空工作表）_\n")
            continue
        parts.append(_rows_to_table(rows, max_cols) + "\n")
        if ws.nrows > max_rows:
            parts.append(f"> 数据超过 {max_rows} 行，已截断。\n")
    return cleanup_markdown("\n".join(parts))


# ---------------- PDF（文本型） ----------------

_LAYOUT_SET: bool | None = None


def _ensure_layout(use_layout: bool) -> None:
    """设置 pymupdf4llm 的版面分析开关（只需生效一次）。"""
    global _LAYOUT_SET
    if _LAYOUT_SET is use_layout:
        return
    try:
        import pymupdf4llm

        pymupdf4llm.use_layout(use_layout)
        _LAYOUT_SET = use_layout
    except Exception:
        pass


def pdf_to_markdown(src: Path, page_chunk: int = 50, use_layout: bool = False) -> str:
    """文本型 PDF → Markdown，走 pymupdf4llm。大文件分块以免内存峰值。

    use_layout=True 会启用 ONNX 版面模型：标题识别略好，但慢十几倍，
    且会拉起子进程、在中文 Windows 下刷编码报错。默认关闭。

    注意：很多"文本型"PDF 其实是扫描件 + OCR 文本层（可搜索 PDF），文本层里
    每个视觉行末尾都是硬换行，pymupdf4llm 对中文不会合并，会产出"一行一段"。
    因此这里统一做一次中文段落重组（merge_pdf_lines）。
    """
    import pymupdf
    import pymupdf4llm

    from .detect import silence_mupdf

    silence_mupdf()
    # 重要：不要为了"提速"而调用 pymupdf4llm.use_layout(False)。
    # pymupdf4llm 1.28 默认 _use_layout=True，而 use_layout(False) 会执行
    # ``pymupdf._get_layout = None``，把 pymupdf 的版面引擎整个摘掉——普通文档
    # 确实快 4~6 倍，但规范/标准类 PDF（复杂版面）会解析出空内容或大量丢失：
    #   实测 GB 50028-2006（190 页）2577 字 vs 69699 字；AQ 2004-2005 0 字 vs 46728 字。
    # 因此仅在显式要求时才切换全局状态，默认沿用 pymupdf4llm 的完整模式。
    if use_layout:
        _ensure_layout(True)
    doc = pymupdf.open(str(src))
    try:
        total = doc.page_count
        if total <= page_chunk:
            md = pymupdf4llm.to_markdown(doc, show_progress=False)
        else:
            parts = []
            for start in range(0, total, page_chunk):
                end = min(start + page_chunk, total)
                part = pymupdf4llm.to_markdown(
                    doc, pages=list(range(start, end)), show_progress=False
                )
                parts.append(part)
            md = "\n\n".join(parts)
        md = merge_pdf_lines(md)
        return cleanup_markdown(md)
    finally:
        doc.close()


# ---------------- 文本型 PDF 的段落重组 ----------------
# 背景：可搜索 PDF（扫描件 + OCR 文本层）的文本层里，每个视觉行末尾都是硬换行。
# pymupdf4llm 面对中文没有拉丁文那种"句末标点 + 大写字母"的换段依据，于是把
# 每个视觉行都当成一个独立段落，产出"一行一段"。这里按中文行文规则把同一自然
# 段内的行重新拼接回去，并顺手清理 pymupdf4llm 在中文里插入的多余空格。

_CJK_RANGE = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_PUNCT = "，。！？；：、（）（）《》【】「」『』“”‘’…—·"
# 句末标点：行尾出现这些，说明该行是本自然段末行
_PDF_TERMINAL = set("。！？；：!?;:")
# 收尾符号（引号/括号等）：判断句末标点时先剥掉
_PDF_CLOSERS = "\"'\"''）)】》」』"
# Markdown 结构行：标题 / 列表 / 引用 / 表格 / 代码围栏，不参与合并
_PDF_STRUCT = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||```)")
# 数字（含全角）
_PDF_DIGITS = r"0-9０-９"
# 独立页码行
_PDF_PAGENUM = re.compile(rf"^[\s\-–—/·]*[{_PDF_DIGITS}]{{1,3}}[\s\-–—/·]*$")
# 目录行：点引导线 + 行末页码，或"章节序号 … 页码"式条目。独立成行，不参与合并。
_PDF_TOC = re.compile(
    rf"([.·…．]{{2,}}\s*[（(]?[{_PDF_DIGITS}]{{1,4}}[)）]?\s*$)"
    rf"|(^\s*(第[一二三四五六七八九十百\d]+[章节篇]"
    rf"|[一二三四五六七八九十]+、"
    rf"|[（(][一二三四五六七八九十\d]+[)）]"
    rf"|\d+[.、])\s*[^。]{{0,60}}\s[（(]?[{_PDF_DIGITS}]{{1,4}}[)）]?\s*$)"
)
# 公文里应当独立成行的元素：附件/联系人/抄送等标签行、落款"单位+日期"行、邮箱行
_PDF_AFFIX = r"(局|厅|部|委|办公室|政府|公司|支队|大队|中队|中心|医院|学校|学院|研究所|站|所|科|处|室|集团)"
_PDF_STANDALONE = re.compile(
    r"^(附\s*件|附\s*录|联系人|联系电话|电话|传真|抄送|抄报|主题词|信息公开选项"
    r"|承办科室|经办人|签发|审核|拟稿|打印|印发|主送|报送|备注|注)\s*[:：]"
    rf"|^[{_PDF_DIGITS}]{{4}}\s*年\s*[{_PDF_DIGITS}]{{1,2}}\s*月\s*[{_PDF_DIGITS}]{{1,2}}\s*日\s*$"
    rf"|^\S{{0,20}}{_PDF_AFFIX}\s*[{_PDF_DIGITS}]{{4}}\s*年\s*[{_PDF_DIGITS}]{{1,2}}\s*月\s*[{_PDF_DIGITS}]{{1,2}}\s*日\s*$"
    r"|^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*$"
)
# 公文名结尾词：附件名/标题以这些收尾时视为"完整元素"，其后应断开
_PDF_DOC_TAIL = re.compile(
    r"(函|通知|报告|方案|意见|规定|办法|条例|目录|清单|台账|纪要|批复|决定|公告|通告"
    r"|请示|议案|说明|标准|规范|计划|总结|制度|协议|合同|决议|汇报|讲话|材料)\s*$"
)

_CJK_CHAR = re.compile(rf"[{_CJK_RANGE}]")


def _is_cjk_ch(ch: str) -> bool:
    return bool(ch) and bool(_CJK_CHAR.match(ch))


def _pdf_ends_sentence(text: str) -> bool:
    t = text.rstrip(_PDF_CLOSERS)
    if not t:
        return False
    return t[-1] in _PDF_TERMINAL


def _pdf_join_sep(a: str, b: str) -> str:
    """两行拼接时的分隔符：中文相邻不加空格，其余加一个空格。"""
    if not a or not b:
        return ""
    la, fb = a[-1], b[0]
    if _is_cjk_ch(la) and _is_cjk_ch(fb):
        return ""
    if _is_cjk_ch(la) and fb in _CJK_PUNCT:
        return ""
    if la in _CJK_PUNCT and _is_cjk_ch(fb):
        return ""
    if (_is_cjk_ch(la) or la in _CJK_PUNCT) and fb.isdigit():
        return ""
    if la.isdigit() and (_is_cjk_ch(fb) or fb in _CJK_PUNCT):
        return ""
    return " "


# 行内多余空格清理（只处理中文相邻场景，不动英文/代码）
_SPACE_RULES = [
    (re.compile(rf"(?<=[{_CJK_RANGE}])\s+(?=[{_CJK_RANGE}])"), ""),
    (re.compile(rf"(?<=[{_CJK_RANGE}])\s+(?=[{re.escape(_CJK_PUNCT)}])"), ""),
    (re.compile(rf"(?<=[{re.escape(_CJK_PUNCT)}])\s+(?=[{_CJK_RANGE}])"), ""),
    (re.compile(rf"(?<=[{re.escape(_CJK_PUNCT)}])\s+(?=[{re.escape(_CJK_PUNCT)}])"), ""),
    (re.compile(rf"(?<=[{_CJK_RANGE}])\s+(?=\d)"), ""),
    (re.compile(rf"(?<=\d)\s+(?=[{_CJK_RANGE}])"), ""),
    (re.compile(rf"(?<=[{re.escape(_CJK_PUNCT)}])\s+(?=\d)"), ""),
    (re.compile(rf"(?<=\d)\s+(?=[{re.escape(_CJK_PUNCT)}])"), ""),
    # 中文与拉丁字母相邻（"豫 A5072V" → "豫A5072V"）
    (re.compile(rf"(?<=[{_CJK_RANGE}{re.escape(_CJK_PUNCT)}])\s+(?=[A-Za-z])"), ""),
    (re.compile(rf"(?<=[A-Za-z])\s+(?=[{_CJK_RANGE}{re.escape(_CJK_PUNCT)}])"), ""),
]

# 中文语境里的 ASCII 标点 → 全角（前后都紧邻中日韩文字时才替换，避开数字/英文）
_PUNCT_FIX = [
    (re.compile(rf"(?<=[{_CJK_RANGE}{re.escape(_CJK_PUNCT)}]),\s*(?=[{_CJK_RANGE}])"), "，"),
    (re.compile(rf"(?<=[{_CJK_RANGE}{re.escape(_CJK_PUNCT)}]),(?=\s*$)"), "，"),
    (re.compile(rf"(?<=[{_CJK_RANGE}{re.escape(_CJK_PUNCT)}]);(?=[{_CJK_RANGE}])"), "；"),
    (re.compile(rf"(?<=[{_CJK_RANGE}{re.escape(_CJK_PUNCT)}]):(?=[{_CJK_RANGE}])"), "："),
    (re.compile(rf"(?<=[{_CJK_RANGE}])\?(?=[{_CJK_RANGE}])"), "？"),
    (re.compile(rf"(?<=[{_CJK_RANGE}])!(?=[{_CJK_RANGE}])"), "！"),
]


def _tidy_cjk_spaces(s: str) -> str:
    for rx, rep in _SPACE_RULES:
        s = rx.sub(rep, s)
    for rx, rep in _PUNCT_FIX:
        s = rx.sub(rep, s)
    return s


def merge_pdf_lines(md: str) -> str:
    """把 pymupdf4llm 产出的"一行一段"按中文段落规则重新聚合。

    pymupdf4llm 对这类文档的换段依据是行末标点 + 行间空行；中文文本层没有
    拉丁文那种"句末标点 + 下一行首字母大写"的特征，于是每个视觉行都被当成
    独立段落，行与行之间用空行隔开（\n\n\n）。因此这里的空行**不是**段落
    边界，真正的依据是行末标点。

    规则：
      - 行尾是句末标点（。！？；：）→ 本段结束。
      - 行尾不是句末标点 → 与下一行同段，直接拼接（中文之间不加空格）。
      - 标题/列表/引用/表格/代码行 → 不参与合并。
      - 独立页码行 → 丢弃（且不打断跨页段落）。
      - 空行 → 忽略；但若上一行很短（<15 字）且无句末标点，视为独立行。
    """
    if not md:
        return md

    blocks: list[tuple[str, bool]] = []  # (文本, 是否自然段)
    buf = ""
    pending_blank = False
    soft = False  # 当前 buf 是"公文标签行"这类软独立元素，允许短续行并入

    def flush() -> None:
        nonlocal buf, pending_blank, soft
        if buf:
            blocks.append((_tidy_cjk_spaces(buf), True))
            buf = ""
        pending_blank = False
        soft = False

    for raw in md.split("\n"):
        line = raw.strip()
        if not line:
            pending_blank = True
            continue
        # 结构行：独立成块，不合并
        if _PDF_STRUCT.match(line):
            flush()
            blocks.append((line, False))
            continue
        # 页码行：丢弃，且不 flush（允许跨页段落继续拼接）
        if _PDF_PAGENUM.match(line):
            continue
        # 目录行（点引导线）：独立成行
        if _PDF_TOC.search(line):
            flush()
            blocks.append((_tidy_cjk_spaces(line), False))
            continue
        # 公文标签行（附件/联系人/落款日期等）：断开前段，但允许短续行并入
        if _PDF_STANDALONE.match(line):
            flush()
            buf = line
            soft = True
            pending_blank = False
            continue
        if not buf:
            buf = line
            pending_blank = False
            continue
        # 段落边界判定
        if (
            _pdf_ends_sentence(buf)
            or _is_heading_or_item(buf)
            or _is_heading_or_item(line)
            or (pending_blank and len(buf) <= 15)
        ):
            flush()
            buf = line
            pending_blank = False
            continue
        # 软独立元素：只有"短续行"才并入，避免把下一条公文元素粘上来
        if soft and (
            len(line) > 12
            or _PDF_STANDALONE.match(line)
            or _PDF_DOC_TAIL.search(buf)
        ):
            flush()
            buf = line
            pending_blank = False
            continue
        buf = buf + _pdf_join_sep(buf, line) + line
        pending_blank = False
    flush()

    # 输出：自然段之间空行；连续结构行紧邻
    out: list[str] = []
    for i, (text, is_para) in enumerate(blocks):
        if i > 0 and (is_para or blocks[i - 1][1]):
            out.append("")
        out.append(text)
    return "\n".join(out)


# ---------------- HTML（伪装成 Office 的文件） ----------------

def html_to_markdown(src: Path) -> str:
    import markdownify

    raw = src.read_bytes()
    text = None
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    md = markdownify.markdownify(text, heading_style="ATX", bullets="-")
    return cleanup_markdown(html_mod.unescape(md))


# ---------------- 纯文本 ----------------

def text_to_markdown(src: Path) -> str:
    raw = src.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return cleanup_markdown(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return cleanup_markdown(raw.decode("utf-8", errors="replace"))


# ---------------- HTML 表格 → GFM 表格 ----------------

_TABLE_BLOCK = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


class _TableExtractor(HTMLParser):
    """从 HTML 里抽出所有表格的行列文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None
        elif tag == "tr" and self._row is not None:
            if self._rows is not None:
                self._rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables_to_markdown(text: str, max_cols: int = 60) -> str:
    """把 PaddleOCR 输出的 <table style=...> 转成标准 GFM 表格。

    OCR 结果里的表格是带大量内联样式的 HTML，直接留在 md 里既臃肿又难读。
    """
    if "<table" not in text.lower():
        return text
    parser = _TableExtractor()
    try:
        parser.feed(text)
    except Exception:
        return text
    if not parser.tables:
        return text

    it = iter(parser.tables)

    def repl(m):
        try:
            rows = next(it)
        except StopIteration:
            return m.group(0)
        table = _rows_to_table(rows, max_cols)
        return f"\n\n{table}\n\n" if table else ""

    return _TABLE_BLOCK.sub(repl, text)


# ---------------- 输出路径 ----------------

def md_path_for(src: Path, output=None, roots: list[str] | None = None) -> Path:
    """计算 Markdown 输出路径。

    默认与原文件同目录同名；当 output.mode == "custom" 时输出到 output.root 下：
      - layout = mirror：保留相对 roots 的目录结构（推荐，天然避免重名）
      - layout = flat  ：全部平铺到根目录
    """
    src = Path(src)
    mode = getattr(output, "mode", "alongside") if output is not None else "alongside"
    root = (getattr(output, "root", "") or "").strip() if output is not None else ""
    if mode != "custom" or not root:
        return src.with_suffix(".md")

    root_p = Path(root)
    layout = getattr(output, "layout", "mirror") if output is not None else "mirror"
    if layout == "flat":
        return root_p / f"{src.stem}.md"

    rel = None
    try:
        src_abs = src.resolve()
    except OSError:
        src_abs = src
    for r in roots or []:
        try:
            rel = src_abs.relative_to(Path(r).resolve())
            break
        except (ValueError, OSError):
            continue
    if rel is None:
        rel = Path(src.name)
    return root_p / rel.with_suffix(".md")


def assets_dir_for(md_path: Path) -> Path:
    """同名 .assets 目录，放 OCR / 内嵌图片（跟随 md 所在位置）。"""
    return Path(md_path).with_suffix(".assets")


def write_markdown(md_path: Path, text: str, title: str | None = None) -> None:
    """写入 Markdown 文件（UTF-8 + BOM 便于 Windows 记事本识别中文）。"""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    body = text
    if title and not re.match(r"^\s*#\s", body):
        body = f"# {title}\n\n{body}"
    md_path.write_text(body, encoding="utf-8-sig")


# ---------------- 启发式表格还原（本地 OCR 坐标 → GFM 表格） ----------------
# 纯坐标聚类，零模型依赖。还原规整表格；遇到列对齐混乱时返回复杂表信号，
# 由引擎据此把该页转发云端 API 兜底。

def _box_center_y(box: list[list[float]]) -> float:
    ys = [p[1] for p in box]
    return sum(ys) / len(ys) if ys else 0.0


def _box_left(box: list[list[float]]) -> float:
    xs = [p[0] for p in box]
    return min(xs) if xs else 0.0


def _box_right(box: list[list[float]]) -> float:
    xs = [p[0] for p in box]
    return max(xs) if xs else 0.0


def _box_height(box: list[list[float]]) -> float:
    ys = [p[1] for p in box]
    return max(ys) - min(ys) if ys else 0.0


def ocr_items_to_markdown(
    items,
    page_height: float = 0.0,
    row_gap_ratio: float = 0.6,
    col_gap_ratio: float = 0.6,
    complex_col_threshold: int = 12,
) -> tuple[str, bool]:
    """把一页的 OCR 文本项（含坐标）重排成 Markdown。

    返回 (markdown, is_complex)。is_complex=True 表示检测到疑似复杂表格
    （列对齐散乱、合并单元格特征），建议转发云端 VLM 保真。

    算法：
      1. 按 y 中心聚类成行（间距小于行高 gap 阈值视为同一行）。
      2. 行内按 x 排序。
      3. 若某行有 >=2 个文本项且各行的 x 边界能稳定对齐成列 → 输出 GFM 表格；
         否则按普通段落输出。
      4. 列边界不稳定（方差过大）→ 判为复杂表。
    """
    if not items:
        return "", False

    # ---- 1. 按 y 聚类成行 ----
    rows: list[list] = []
    for it in items:
        cy = _box_center_y(it.box)
        h = _box_height(it.box) or 1.0
        placed = False
        for row in rows:
            row_cy = sum(_box_center_y(r.box) for r in row) / len(row)
            row_h = sum(_box_height(r.box) for r in row) / len(row) or 1.0
            if abs(cy - row_cy) <= max(h, row_h) * row_gap_ratio:
                row.append(it)
                placed = True
                break
        if not placed:
            rows.append([it])

    # 行内按 x 排序
    for row in rows:
        row.sort(key=lambda r: _box_left(r.box))

    # ---- 2. 判断是否像表格 ----
    # 表格式样：至少 2 行，且每行 >=2 项
    multi_item_rows = [r for r in rows if len(r) >= 2]
    is_table_like = len(multi_item_rows) >= 2

    if not is_table_like:
        # 普通文本：先聚合段落，再输出
        lines = _merge_paragraphs(rows, page_height)
        return "\n\n".join(lines), False

    # ---- 3. 列边界对齐分析 ----
    # 用每行各项的 x 中心做一维聚类，得到全局列边界
    all_centers = sorted(_box_center_x(it.box) for row in multi_item_rows for it in row)
    col_edges = _cluster_1d(all_centers, col_gap_ratio)
    n_cols = len(col_edges)

    # 复杂表信号：列数异常多（>8 列可能合并单元格）、或各行项数差异大
    row_item_counts = [len(r) for r in multi_item_rows]
    count_std = _std(row_item_counts)
    mean_count = sum(row_item_counts) / len(row_item_counts)

    if n_cols > complex_col_threshold or count_std > mean_count * 0.6 + 1:
        # 结构太乱，判定复杂，交云端兜底；本地仍尽量输出普通文本
        lines = _merge_paragraphs(rows, page_height)
        return "\n\n".join(lines), True

    # ---- 4. 按列归位 ----
    table_rows = []
    for row in rows:
        cells = [""] * n_cols
        for it in row:
            cx = _box_center_x(it.box)
            col = _nearest_col(cx, col_edges)
            if cells[col]:
                cells[col] += " " + it.text
            else:
                cells[col] = it.text
        table_rows.append(cells)

    # 首行当表头
    header = table_rows[0]
    out = []
    out.append("| " + " | ".join(_cell_to_text(c) for c in header) + " |")
    out.append("|" + "---|" * n_cols)
    for r in table_rows[1:]:
        out.append("| " + " | ".join(_cell_to_text(c) for c in r) + " |")
    return "\n".join(out), False


def _box_center_x(box: list[list[float]]) -> float:
    xs = [p[0] for p in box]
    return sum(xs) / len(xs) if xs else 0.0


# ---------------- 段落聚合（把 OCR 逐行文本还原成自然段） ----------------

# 段落结束标点：行尾出现这些通常意味着该行是段落末行
_SENT_END = set("。！？；：!?;:”’』）】》…")
# 段落起始标记：以这些开头的行，几乎肯定是新段落/标题/条目，独立成行
_HEADING_PREFIX = re.compile(
    r"^(\s*(第[一二三四五六七八九十百千\d]+[章节部分篇]|[一二三四五六七八九十]+、|"
    r"（[一二三四五六七八九十]+）|\(\s*[一二三四五六七八九十]+\s*\)|\d+[\.、．)]"
    r"|[（(][一二三四五六七八九十\d]+[)）]\s*))"
)
# 纯数字/页码（可能带两侧空白，如 "3"、"- 3 -"、"3 / 5"）
_PAGE_NUM = re.compile(r"^[\s\-–—/·]*\d+[\s\-–—/·]*(\d+)?[\s\-–—/·]*$")


def _is_heading_or_item(text: str) -> bool:
    """是否像标题/列表项/短条目，应独立成行。"""
    t = text.strip()
    if not t:
        return False
    if len(t) <= 18 and _HEADING_PREFIX.match(t):
        return True
    return False


def _looks_like_page_number(text: str) -> bool:
    """孤立数字项（页码/脚注序号）。"""
    t = text.strip()
    if not t:
        return False
    if not _PAGE_NUM.match(t):
        return False
    # 单个纯数字或形如 "3" / "- 3 -" / "3 / 5"
    return True


def _is_orphan_tail(line: dict, all_lines: list[dict]) -> bool:
    """判断某行是否是"行末残字"：文本极短且右边界明显短于正文行。

    OCR 渲染分页边界处，最后半行常被单独切成一个高文本框（如"行。"），
    右边界远未到正文右边界。这类残行不应视为标题。
    """
    t = line["text"].strip()
    if len(t) > 4:
        return False
    # 正文行的典型右边界：取右边界最大的一批行的中位数
    rights = sorted(l["right"] for l in all_lines)
    if not rights:
        return False
    body_right = rights[len(rights) * 3 // 4] if rights else line["right"]
    return line["right"] < body_right * 0.6


def _merge_paragraphs(
    rows: list[list], page_height: float = 0.0
) -> list[str]:
    """把按 y 聚类后的 OCR 行聚合成自然段。

    断段信号（任一命中即断）：
      1. 行间距明显大于行内间距（段间 gap 通常 > 1.8 倍行高）。
      2. 上一行行尾是句子结束标点（。！？；：等）。
      3. 当前行是标题/列表项/短条目。
      4. 当前行行首 x 明显右移（新段首行缩进，> 1.2 倍字符宽）。
    另：过滤掉孤立的页码数字。
    """
    if not rows:
        return []

    # 预处理：过滤页码项，得到带坐标的行描述
    lines = []
    for row in rows:
        text = " ".join(it.text for it in row).strip()
        if not text:
            continue
        left = min(_box_left(it.box) for it in row)
        right = max(_box_right(it.box) for it in row)
        cy = sum(_box_center_y(it.box) for it in row) / len(row)
        h = max(_box_height(it.box) for it in row) or 1.0

        # 页码过滤：纯数字、且位于页面底部 10% 或（居中/靠右）
        if _looks_like_page_number(text):
            is_bottom = page_height > 0 and cy > page_height * 0.88
            if is_bottom or len(text) <= 3:
                continue
        lines.append({"text": text, "left": left, "right": right,
                      "cy": cy, "h": h})

    if not lines:
        return []

    # 正文基准行高：取所有行高的中位数（标题/字号异常行是少数，不影响中位）
    heights = sorted(l["h"] for l in lines)
    body_h = heights[len(heights) // 2] if heights else 1.0

    paras: list[str] = []
    cur = [lines[0]]
    cur_h = lines[0]["h"]

    def _flush():
        if not cur:
            return
        text = " ".join(l["text"] for l in cur)
        # 标题判定：独立单行，且
        #   a) 中文序号标题（一、/（一）/1. 等），或
        #   b) 大字号（行高 > 正文 1.25 倍）且文本长度适中（3~40 字）
        # 排除行末残字（极短 + 右边界远短于正文）。
        is_title = False
        if len(cur) == 1 and not _is_orphan_tail(cur[0], lines):
            t = text.strip()
            avg_h = cur[0]["h"]
            if len(t) >= 3 and _is_heading_or_item(t):
                is_title = True
            elif avg_h > body_h * 1.25 and 3 <= len(t) <= 40:
                is_title = True
        paras.append(("# " + text) if is_title else text)

    for prev, nxt in zip(lines, lines[1:]):
        gap = nxt["cy"] - prev["cy"]
        gap_ratio = gap / max(cur_h, 1.0)
        indent = nxt["left"] - prev["left"]
        char_w = prev["h"]  # 用行高近似一个字符宽
        prev_ends_sent = prev["text"][-1:] in _SENT_END

        # 信号 1：行间距显著拉大（真正的段间空档）→ 断段
        if gap_ratio > 2.2:
            _flush()
            cur = [nxt]
            cur_h = nxt["h"]
            continue

        # 信号 3：当前行是标题/列表项/短条目 → 断段
        if _is_heading_or_item(nxt["text"]):
            _flush()
            cur = [nxt]
            cur_h = nxt["h"]
            continue

        # 信号 3b：当前行或上一行是大字号行（标题/副标题）→ 各自独立
        #   但排除"行末残字"：大字号且右边界远未到正文右边界的，是 OCR
        #   渲染分页截断产生的残行，不是标题。
        def _big(hv: float) -> bool:
            return hv > body_h * 1.25

        nxt_big = _big(nxt["h"]) and not _is_orphan_tail(nxt, lines)
        prev_big = _big(prev["h"]) and not _is_orphan_tail(prev, lines)
        if nxt_big or prev_big:
            _flush()
            cur = [nxt]
            cur_h = nxt["h"]
            continue

        # 信号 4：当前行行首明显右移（段首缩进）→ 断段
        #   这是中文公文/报告最可靠的段首标志（正文每段首行空两格）
        if indent > char_w * 1.2:
            _flush()
            cur = [nxt]
            cur_h = nxt["h"]
            continue

        # 信号 2（弱）：上一行以句末标点结尾，且下一行有轻微缩进或间距略大
        #   单纯行尾句号不断（避免误断行内换行处恰好带标点的场景）
        if prev_ends_sent and (indent > char_w * 0.5 or gap_ratio > 1.6):
            _flush()
            cur = [nxt]
            cur_h = nxt["h"]
            continue

        # 否则：同段续接
        cur.append(nxt)
        cur_h = max(cur_h, nxt["h"])

    _flush()
    return paras


def _std(vals: list[float]) -> float:
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _cluster_1d(values: list[float], gap_ratio: float) -> list[float]:
    """一维聚类，返回各簇的中心。gap_ratio 相对相邻值间距的合并阈值。"""
    if not values:
        return []
    clusters = [[values[0]]]
    for v in values[1:]:
        last_center = sum(clusters[-1]) / len(clusters[-1])
        gap = v - last_center
        # 相邻簇间距阈值：用已有簇内平均间距估算
        if gap > _estimate_gap(clusters[-1], gap_ratio):
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]


def _estimate_gap(cluster: list[float], gap_ratio: float) -> float:
    """估算"是否该开新列"的间距阈值。"""
    if len(cluster) < 2:
        return 10.0
    sorted_c = sorted(cluster)
    gaps = [b - a for a, b in zip(sorted_c, sorted_c[1:])]
    avg_gap = sum(gaps) / len(gaps)
    # 用簇内字符间距估计一个字符宽，列间距通常远大于字符间距
    return max(avg_gap * 3, 15.0)


def _nearest_col(cx: float, col_edges: list[float]) -> int:
    return min(range(len(col_edges)), key=lambda i: abs(col_edges[i] - cx))
