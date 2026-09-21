"""格式探测：按文件头魔数判断真实格式，避免被错误扩展名误导。

实测本工作区里约 47% 的 .docx 实际是老式 OLE2 的 .doc，必须靠魔数分流。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

# 魔数签名
ZIP_SIG = b"PK\x03\x04"
OLE2_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PDF_SIG = b"%PDF"
BMP_SIG = b"BM"
PNG_SIG = b"\x89PNG"
JPEG_SIG = b"\xff\xd8\xff"
TIFF_LE = b"II*\x00"
TIFF_BE = b"MM\x00*"
GIF_SIG = b"GIF8"
RTF_SIG = b"{\\rtf"
OFD_SIG = b"PK\x03\x04"  # OFD 也是 zip，靠内部结构区分，此处不深究


class Kind(str, Enum):
    DOCX = "docx"          # OOXML 文档（zip）
    XLSX = "xlsx"          # OOXML 表格（zip）
    PPTX = "pptx"
    ZIP_UNKNOWN = "zip?"   # zip 但内部结构未知
    DOC = "doc"            # OLE2 老式 Word
    XLS = "xls"            # OLE2 老式 Excel
    PPT = "ppt"
    OLE_UNKNOWN = "ole?"
    PDF = "pdf"
    IMAGE = "image"
    HTML = "html"
    RTF = "rtf"
    TEXT = "text"
    UNKNOWN = "unknown"
    MISSING = "missing"


_IMAGE_SIGS = (BMP_SIG, PNG_SIG, JPEG_SIG, TIFF_LE, TIFF_BE, GIF_SIG)


def sniff(path: str | Path) -> Kind:
    """读取文件头判断真实类型。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return Kind.MISSING
    try:
        with open(p, "rb") as f:
            head = f.read(512)
    except OSError:
        return Kind.MISSING
    if not head:
        return Kind.UNKNOWN

    if head.startswith(PDF_SIG):
        return Kind.PDF
    if head.startswith(OLE2_SIG):
        return _classify_ole2(p)
    if any(head.startswith(s) for s in _IMAGE_SIGS):
        return Kind.IMAGE
    if head.startswith(RTF_SIG):
        return Kind.RTF
    if head.startswith(ZIP_SIG):
        return _classify_zip(p)
    stripped = head.lstrip()[:64].lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html") or b"<head" in stripped:
        return Kind.HTML
    return Kind.UNKNOWN


def _classify_zip(p: Path) -> Kind:
    """打开 zip 看内部结构判断是 docx / xlsx / pptx。"""
    import zipfile

    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
    except Exception:
        return Kind.ZIP_UNKNOWN
    if "word/document.xml" in names:
        return Kind.DOCX
    if "xl/workbook.xml" in names or any(n.startswith("xl/") for n in names):
        return Kind.XLSX
    if any(n.startswith("ppt/") for n in names):
        return Kind.PPTX
    return Kind.ZIP_UNKNOWN


def _classify_ole2(p: Path) -> Kind:
    """OLE2 容器里靠 Stream 名区分 Word / Excel / PowerPoint。

    OLE2 的目录项用 UTF-16LE 存 Stream 名，直接在原始字节里搜关键词即可。
    """
    try:
        with open(p, "rb") as f:
            blob = f.read(16384)
    except OSError:
        return Kind.OLE_UNKNOWN

    def has(name: str) -> bool:
        return name.encode("utf-16-le") in blob

    if has("WordDocument"):
        return Kind.DOC
    if has("Workbook") or has("Book"):
        return Kind.XLS
    if has("PowerPoint Document"):
        return Kind.PPT
    # 退一步：扩展名兜底
    ext = p.suffix.lower()
    if ext in (".doc", ".docx", ".wps"):
        return Kind.DOC
    if ext in (".xls", ".xlsx", ".et"):
        return Kind.XLS
    return Kind.OLE_UNKNOWN


# ---------------- PDF 文本层判断 ----------------

_MUPDF_QUIET = False


def silence_mupdf() -> None:
    """关掉 PyMuPDF 往 stderr 打的原生报错。

    扫描件里混杂的破损 PDF 会触发 "MuPDF error: format error: ..." 之类的
    原生输出，看着吓人但不影响处理，这里统一静音。
    """
    global _MUPDF_QUIET
    if _MUPDF_QUIET:
        return
    try:
        import pymupdf

        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
        _MUPDF_QUIET = True
    except Exception:
        pass


def pdf_text_profile(
    path: str | Path, probe_pages: int = 5
) -> tuple[int, int, int]:
    """返回 (总页数, 探测页数, 探测页总字符数)。

    用于判断 PDF 是文本型还是扫描型。字符数过少说明是图片扫描件。
    """
    import pymupdf

    silence_mupdf()
    doc = pymupdf.open(str(path))
    try:
        total = doc.page_count
        n = min(probe_pages, total)
        chars = 0
        for i in range(n):
            chars += len(doc[i].get_text().strip())
        return total, n, chars
    finally:
        doc.close()


def is_text_pdf(
    path: str | Path, min_chars_per_page: int = 80, probe_pages: int = 5
) -> tuple[bool, int, int]:
    """判断 PDF 是否文本型，返回 (是否文本型, 总页数, 探测字符数)。"""
    total, n, chars = pdf_text_profile(path, probe_pages)
    per_page = chars / n if n else 0
    return per_page >= min_chars_per_page, total, chars


# ---------------- PDF 文本层"可信度"判断 ----------------
# 只看字数的 is_text_pdf 会漏掉一大类：**扫描件 + OCR 文本层**（俗称可搜索 PDF）。
# 它们的文本层是 OCR 软件生成的，字符数看着够，实际常有三类毛病：
#   1. 字体缺 ToUnicode 映射 → 抽出来是 CID 乱码（实测 GB 50011 有 7% 怪字）；
#   2. 文本是"隐形"的（render mode 3 / opacity 0），只用来做搜索；
#   3. 整页是扫描图像，文字层只是机器识别结果，标点/断行/生僻字都不可靠。
# 这类文件应当改走 OCR 路线重新识别，而不是直接抽文本层。

_PROBE_TOL_WEIRD = 0.10      # 单页"怪字"比例超过此值即认为该页文本层乱码
_PROBE_TOL_INVISIBLE = 0.5   # 隐形字符比例超过此值即认为该页是 OCR 搜索层
_PROBE_IMG_COVER = 0.6       # 单页图片覆盖比例，超过即视为"扫描页"
_PROBE_MIN_POINTS = 9        # 最少探测页数（5 个取样点容易全落在空白页上，误判"无文本层"）
_PROBE_VOTE = 0.6            # 多大比例的探测页满足条件才定性


def _vote(hits: int, n: int, ratio: float = _PROBE_VOTE) -> bool:
    """票数是否达到定性比例（至少 1 票）。"""
    import math

    return hits >= max(1, math.ceil(n * ratio))


def _page_signals(page) -> tuple[int, float, float, float]:
    """返回单页 (有效字符数, 整页图覆盖比例, 隐形字符比例, 怪字比例)。"""
    txt = page.get_text()
    chars = len(txt.strip())

    parea = abs(page.rect.width * page.rect.height) or 1.0
    cover = 0.0
    try:
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                cover = max(cover, abs(r.width * r.height) / parea)
    except Exception:  # noqa: BLE001
        pass

    inv = vis = 0
    try:
        for span in page.get_texttrace():
            s = span.get("text", "")
            if isinstance(s, bytes):
                s = s.decode("utf-16-le", "ignore")
            c = len(s.strip())
            if span.get("opacity", 1) == 0 or span.get("type") == 3:
                inv += c
            else:
                vis += c
    except Exception:  # noqa: BLE001
        vis = chars
    invisible = inv / max(inv + vis, 1)

    bad = 0
    for ch in txt:
        o = ord(ch)
        if ch in "\n\r\t":
            continue
        if o < 0x20 or o == 0xFFFD or 0xE000 <= o <= 0xF8FF:
            bad += 1
    weird = bad / max(chars, 1)

    return chars, cover, invisible, weird


def pdf_text_trust(
    path: str | Path, min_chars_per_page: int = 80, probe_pages: int = 5
) -> tuple[bool, int, str]:
    """判断 PDF 文本层是否**可信**。返回 (是否可信, 总页数, 原因)。

    探测页在全文里均匀取样（而不是只看头几页）——实测有文档首页正常、
    后面全是乱码，只看前 5 页会漏判；也有文档取样点恰好落在空白页上，
    所以最少取 9 页并按"票数"定性，而不是看中位数。

    任何异常都退回"按字数判断"的老逻辑，保证不会因为探测失败而误判。
    """
    import pymupdf

    silence_mupdf()
    try:
        doc = pymupdf.open(str(path))
    except Exception:  # noqa: BLE001
        ok, total, _chars = is_text_pdf(path, min_chars_per_page, probe_pages)
        return ok, total, "读取失败，按字数判断"
    try:
        total = doc.page_count
        if total <= 0:
            return False, 0, "空文档"
        n_probe = max(min(_PROBE_MIN_POINTS, total), 2)
        idxs = sorted({
            min(total - 1, max(0, round(i * (total - 1) / (n_probe - 1))))
            for i in range(n_probe)
        })
        sig = [_page_signals(doc[i]) for i in idxs]
    except Exception:  # noqa: BLE001
        ok, total, _chars = is_text_pdf(path, min_chars_per_page, probe_pages)
        return ok, total, "探测失败，按字数判断"
    finally:
        doc.close()

    if not sig:
        return False, total, "空文档"

    n = len(sig)
    low = sum(1 for s in sig if s[0] < min_chars_per_page)
    garbled = sum(1 for s in sig if s[3] > _PROBE_TOL_WEIRD)
    invisible = sum(1 for s in sig if s[2] > _PROBE_TOL_INVISIBLE)
    img_pages = sum(1 for s in sig if s[1] > _PROBE_IMG_COVER)
    med_chars = sorted(s[0] for s in sig)[n // 2]

    if _vote(low, n):
        return False, total, f"无可用文本层（{low}/{n} 页不足 {min_chars_per_page} 字）"
    if _vote(garbled, n, 0.5):
        return False, total, f"文本层乱码（{garbled}/{n} 页异常字符超阈值，字体缺 ToUnicode）"
    if _vote(invisible, n, 0.5):
        return False, total, f"隐形文本层（{invisible}/{n} 页文字不可见，OCR 搜索层）"
    if _vote(img_pages, n):
        return False, total, f"扫描页+OCR 文本层（{img_pages}/{n} 探测页为整页图片）"
    return True, total, f"可信文本层（中位 {med_chars:.0f} 字/页，{n} 页取样）"


def is_stable(path: str | Path, wait: float = 1.5) -> bool:
    """判断文件是否已写入完成（大小不再变化）。用于监控模式防抖。"""
    import time

    p = Path(path)
    try:
        s1 = p.stat().st_size
    except OSError:
        return False
    time.sleep(wait)
    try:
        s2 = p.stat().st_size
    except OSError:
        return False
    return s1 == s2 and s2 > 0
