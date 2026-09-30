"""格式探测：按文件头魔数判断真实格式，避免被错误扩展名误导。

实测本工作区里约 47% 的 .docx 实际是老式 OLE2 的 .doc，必须靠魔数分流。
"""
from __future__ import annotations

import re
import zipfile
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


# ---------------- 加密（受密码保护）识别 ----------------
# 设了「打开密码」的 Office 文件在磁盘上有两种长相，光看文件头与正常文件
# 完全一样，必须往里读一层才能认出来：
#
#   1. **加密的 OOXML**（.docx/.xlsx/.pptx 设了打开密码）
#      —— 外层其实是个 OLE2 容器，里面装着 `EncryptedPackage`（密文本身）
#      与 `EncryptionInfo`（加密参数）两个流。所以它**魔数是 OLE2**，
#      扩展名却是 .docx，`sniff()` 会按扩展名兜底判成 DOC 而送进 COM，
#      然后 WPS 报一句「文档打开失败」—— 看着像文件损坏，其实是缺密码。
#
#   2. **老式 .doc/.xls 设了打开密码** —— 仍是对应格式，但
#      Word 靠 FIB 的 fEncrypted 位标记、Excel 靠 BIFF 的 FILEPASS 记录标记。
#
# 认出来的价值有两个：一是不必去调慢且会报错的 COM，二是不把
# 「缺密码」这种**不可能靠重试解决**的情况混进「失败」里反复重跑。

_CFB_ENDOFCHAIN = 0xFFFFFFFE
_CFB_FREESECT = 0xFFFFFFFF
_CFB_DIFAT_IN_HEADER = 109
# 探测时最多读这么多字节。CFB 的头、DIFAT、FAT、目录项以及本文要读的那
# 几十字节流（FIB / BIFF 头部）几乎总落在这个范围内；超出的部分读不到就
# 判定"探测不出来"，由 COM 报错分类兜底 —— 绝不为了探测把整个大文件读一遍。
_ENCRYPT_PROBE_BYTES = 4 * 1024 * 1024

# 加密 OOXML 里的标志性流名
_ENCRYPTED_STREAM_NAMES = frozenset({
    "EncryptedPackage", "EncryptionInfo", "EncryptedSummary",
})
_WORD_FIB_SIG = 0xA5EC        # WordDocument 流开头的 wIdent
_FIB_FENCRYPTED = 0x0100      # FIB.flags 的 bit 8：文档已加密
_BIFF_FILEPASS = 0x002F       # BIFF 记录：工作簿有打开密码
_BIFF_BOF = 0x0809


class CfbReader:
    """最小 OLE2/CFB 只读解析器：列出目录项、读某个流的前几字节。

    只实现读所需的必要部分（头、DIFAT、FAT、miniFAT、目录项、流链）。
    任何异常都直接抛给调用方，由调用方当作"探测不出来"处理 ——
    探测失败绝不能让转换流程崩掉。
    """

    def __init__(self, data: bytes):
        if len(data) < 512 or data[:8] != OLE2_SIG:
            raise ValueError("不是 OLE2/CFB 文件")
        self.data = data
        shift = int.from_bytes(data[0x1E:0x20], "little")
        mini_shift = int.from_bytes(data[0x20:0x22], "little")
        if not (7 <= shift <= 20) or not (2 <= mini_shift <= shift):
            raise ValueError("扇区大小异常")
        self.sect_size = 1 << shift
        self.mini_size = 1 << mini_shift
        self.n_fat = int.from_bytes(data[0x2C:0x30], "little")
        self.dir_start = int.from_bytes(data[0x30:0x34], "little")
        self.cutoff = int.from_bytes(data[0x38:0x3C], "little")
        self.minifat_start = int.from_bytes(data[0x3C:0x40], "little")
        self.n_minifat = int.from_bytes(data[0x40:0x44], "little")
        self.difat_start = int.from_bytes(data[0x44:0x48], "little")
        self.n_difat = int.from_bytes(data[0x48:0x4C], "little")
        if self.sect_size <= 0 or self.sect_size > (1 << 20):
            raise ValueError("扇区大小异常")
        self._fat = self._load_fat()
        self._minifat = self._load_minifat()
        self._entries = self._load_dir()
        self._mini_stream: bytes | None = None

    # ---------- 底层 ----------
    def _sector(self, i: int) -> bytes:
        off = 512 + i * self.sect_size
        chunk = self.data[off:off + self.sect_size]
        if len(chunk) < self.sect_size:
            raise ValueError("扇区超出已读取范围")
        return chunk

    def _chain(self, fat: list[int], start: int, limit: int = 1 << 22) -> list[int]:
        out: list[int] = []
        cur = start
        seen: set[int] = set()
        while cur not in (_CFB_ENDOFCHAIN, _CFB_FREESECT):
            if cur in seen or cur >= len(fat) or len(out) > limit:
                # 成环 / 越界：当作链到此为止，而不是死循环
                break
            seen.add(cur)
            out.append(cur)
            cur = fat[cur]
        return out

    def _load_fat(self) -> list[int]:
        difat: list[int] = []
        for i in range(_CFB_DIFAT_IN_HEADER):
            v = int.from_bytes(self.data[0x4C + i * 4:0x50 + i * 4], "little")
            if v != _CFB_FREESECT:
                difat.append(v)
        # DIFAT 扩展扇区（大文件才有）
        cur = self.difat_start
        for _ in range(min(self.n_difat, 256)):
            if cur in (_CFB_ENDOFCHAIN, _CFB_FREESECT):
                break
            try:
                sect = self._sector(cur)
            except ValueError:
                break
            for i in range(self.sect_size // 4 - 1):
                v = int.from_bytes(sect[i * 4:i * 4 + 4], "little")
                if v != _CFB_FREESECT:
                    difat.append(v)
            cur = int.from_bytes(sect[-4:], "little")
        fat: list[int] = []
        n_fat = max(1, self.n_fat) if self.n_fat else len(difat)
        for s in difat[:n_fat]:
            try:
                sect = self._sector(s)
            except ValueError:
                break
            for j in range(self.sect_size // 4):
                fat.append(int.from_bytes(sect[j * 4:j * 4 + 4], "little"))
        if not fat:
            raise ValueError("读不到 FAT")
        return fat

    def _load_minifat(self) -> list[int]:
        if self.n_minifat <= 0 or self.minifat_start in (_CFB_ENDOFCHAIN, _CFB_FREESECT):
            return []
        out: list[int] = []
        try:
            for s in self._chain(self._fat, self.minifat_start):
                sect = self._sector(s)
                for j in range(self.sect_size // 4):
                    out.append(int.from_bytes(sect[j * 4:j * 4 + 4], "little"))
        except ValueError:
            return []
        return out

    def _load_dir(self) -> list[dict]:
        entries: list[dict] = []
        for s in self._chain(self._fat, self.dir_start):
            try:
                sect = self._sector(s)
            except ValueError:
                break
            for k in range(0, self.sect_size, 128):
                e = sect[k:k + 128]
                if len(e) < 128:
                    break
                nlen = int.from_bytes(e[0x40:0x42], "little")
                if not (0 < nlen <= 64):
                    continue
                try:
                    name = e[:nlen - 2].decode("utf-16-le", "ignore")
                except Exception:
                    continue
                entries.append({
                    "name": name,
                    "type": e[0x42],
                    "start": int.from_bytes(e[0x74:0x78], "little"),
                    "size": int.from_bytes(e[0x78:0x80], "little"),
                })
        if not entries:
            raise ValueError("读不到目录项")
        return entries

    # ---------- 对外 ----------
    def names(self) -> list[str]:
        return [e["name"] for e in self._entries]

    def find(self, name: str) -> dict | None:
        for e in self._entries:
            if e["name"] == name:
                return e
        return None

    def _read_chain(self, fat: list[int], start: int, size: int) -> bytes:
        """沿 FAT 链拼出主扇区里的流内容（最多 size 字节，size=0 表示全读）。"""
        buf = bytearray()
        for s in self._chain(fat, start):
            buf += self._sector(s)
            if size and len(buf) >= size:
                break
        return bytes(buf[:size]) if size else bytes(buf)

    def _mini_container(self) -> bytes:
        """mini 流的宿主 = 根目录项的流（小于 cutoff 的流都寄存在它里面）。"""
        if self._mini_stream is not None:
            return self._mini_stream
        root = self._entries[0]
        if root["size"] <= 0:
            raise ValueError("没有根流")
        self._mini_stream = self._read_chain(self._fat, root["start"], root["size"])
        return self._mini_stream

    def stream(self, name: str, max_bytes: int = 4096) -> bytes | None:
        """读指定流的前 max_bytes 字节；流不存在返回 None。"""
        e = self.find(name)
        if e is None or e["type"] != 2:
            return None
        size = e["size"]
        want = min(size, max_bytes) if size else max_bytes
        if size and size < self.cutoff:
            # 小流寄存在根流里，走 miniFAT
            mini = self._mini_container()
            buf = bytearray()
            for s in self._chain(self._minifat, e["start"]):
                off = s * self.mini_size
                buf += mini[off:off + self.mini_size]
                if len(buf) >= want:
                    break
            return bytes(buf[:want])
        return self._read_chain(self._fat, e["start"], want)


def _biff_has_filepass(stream: bytes) -> bool:
    """在 BIFF 记录流里找 FILEPASS（工作簿打开密码的标志）。

    只走开头的记录 —— FILEPASS 总是紧跟在 BOF 之后，不必扫全流。
    """
    pos = 0
    for _ in range(40):
        if pos + 4 > len(stream):
            return False
        wtype = int.from_bytes(stream[pos:pos + 2], "little")
        cb = int.from_bytes(stream[pos + 2:pos + 4], "little")
        if wtype == _BIFF_FILEPASS:
            return True
        if wtype != _BIFF_BOF and pos > 0 and wtype == 0x000A:  # EOF
            return False
        pos += 4 + cb
    return False


def encryption_reason(path: str | Path) -> str | None:
    """文件是否受「打开密码」保护。是则返回中文原因，否则 None。

    只对 OLE2 外壳的文件有意义（加密 OOXML 与老式 .doc/.xls 都是 OLE2），
    调用方应先看 `sniff()` 的结果再决定要不要问 —— 免得对着普通 docx 白读一遍。
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            data = f.read(min(size, _ENCRYPT_PROBE_BYTES))
    except OSError:
        return None
    if len(data) < 512 or data[:8] != OLE2_SIG:
        return None

    try:
        cfb = CfbReader(data)
    except Exception:  # noqa: BLE001
        # 结构读不动（大文件被截断、非标准 CFB…）：退一步只做关键字扫描。
        # 只认「加密标志流名」这一个强特征，见不到就当没加密、
        # 交给正常转换流程（并由 COM 报错分类兜底）。
        for nm in _ENCRYPTED_STREAM_NAMES:
            if nm.encode("utf-16-le") in data:
                return "文件已加密（设了打开密码），需先去掉密码才能转换"
        return None

    try:
        names = cfb.names()
        if _ENCRYPTED_STREAM_NAMES & set(names):
            return "文件已加密（设了打开密码），需先去掉密码才能转换"
        if "WordDocument" in names:
            fib = cfb.stream("WordDocument", 32) or b""
            if len(fib) >= 12 and int.from_bytes(fib[0:2], "little") == _WORD_FIB_SIG:
                if int.from_bytes(fib[10:12], "little") & _FIB_FENCRYPTED:
                    return "Word 文档已加密（设了打开密码），需先去掉密码才能转换"
        for wb_name in ("Workbook", "Book"):
            if wb_name in names:
                head = cfb.stream(wb_name, 2048) or b""
                if _biff_has_filepass(head):
                    return "Excel 工作簿已加密（设了打开密码），需先去掉密码才能转换"
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------- 空文档识别 ----------------
# z.read() 出来的是字节，不能用 `<w:t` 做子串判断 —— 它会误中
# `<w:tab/>`、`<w:tbl>`、`<w:tc>`、`<w:tr>` 这些同样以 `<w:t` 开头的标签，
# 于是把空文档判成"有内容"。必须要求标签在 t 之后立刻收尾或跟属性。
_TEXT_TAG_RE = re.compile(rb"<(?:w|a):t[\s/>]")


def docx_is_empty(path: str | Path) -> bool | None:
    """docx 是否**通篇没有任何正文文本、也没有插图**。

    只看 `word/` 下的部件：任何一处出现 `<w:t>`（页眉、页脚、脚注里的文字
    也算）或存在媒体文件，就不算空。返回 None 表示"判断不了"（不是合法
    zip 等）—— 那种情况交给正常转换流程去报错，不要替它下结论。

    为什么值得单独判：WPS 会保存出结构完整、但正文为空的壳文件。
    这种文件转换结果必然为空，属于**源文件没内容**，不是转换失败。
    """
    p = Path(path)
    try:
        with zipfile.ZipFile(p) as z:
            for name in z.namelist():
                if not name.startswith("word/"):
                    continue
                if "/media/" in name:
                    return False
                if not name.endswith(".xml"):
                    continue
                try:
                    if _TEXT_TAG_RE.search(z.read(name)):
                        return False
                except Exception:  # noqa: BLE001
                    return None
    except Exception:  # noqa: BLE001
        return None
    return True

