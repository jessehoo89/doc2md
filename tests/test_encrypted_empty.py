# -*- coding: utf-8 -*-
"""空文档 / 加密文件的归类测试。

## 为什么要有这个测试

`state.db` 里那 9 个"失败"文件，逐个挖下去发现**没有一个是真正的转换失败**：

  · 7 个 `解析结果为空` —— 是 WPS 存出来的**空壳 docx**：包结构完整，
    但 `word/document.xml` 里连一个 `<w:t>` 都没有，也没有任何插图。
    源文件本身没内容，转出来必然是空的。
  · 1 个 `.docx` —— 其实是**加密的 OOXML**（OLE2 外壳里装
    `EncryptedPackage` + `EncryptionInfo`），只是扩展名还叫 .docx。
  · 1 个 `.doc` —— 老式 Word 文档，FIB 的 fEncrypted 位为真，**设了打开密码**。

这三类都会被当成 `failed`，于是永远躺在失败列表里、每次 retry 都再报一次，
把真正需要修的问题淹掉。本测试锁定的行为就是：**它们必须是 skipped，不是 failed。**

## 夹具全是现场造的

不依赖任何个人语料路径：OLE2 容器与空壳 docx 都在临时目录里现搭
（见 `_cfb_build`）。这样仓库公开、换台机器都能跑。

## 用法

    python tests/test_encrypted_empty.py
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = Path(ROOT) / "config.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import os  # noqa: E402

os.environ["DOC2MD_ENV_FILE"] = "none"   # 隔离真实凭据，本测试不联网

LOG_PATH = Path(__file__).with_name("test_encrypted_empty.log")

FAILURES = 0


class _Tee:
    def __init__(self, stream, path: Path):
        self._stream = stream
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", encoding="utf-8")

    def write(self, s):
        self._stream.write(s)
        self._file.write(s)
        return len(s)

    def flush(self):
        self._stream.flush()
        self._file.flush()


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILURES
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILURES += 1
        print(f"  [FAIL] {name}  {detail}")


# ============================================================================
#  夹具构造
# ============================================================================

SECT = 512
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD


def _dir_entry(name: str, obj_type: int, start: int, size: int) -> bytes:
    """一个 128 字节的 CFB 目录项。"""
    e = bytearray(128)
    raw = name.encode("utf-16-le") + b"\x00\x00"
    e[0:len(raw)] = raw
    struct.pack_into("<H", e, 0x40, len(raw))          # 名字字节数（含结尾 0）
    e[0x42] = obj_type                                  # 5=根 2=流
    e[0x43] = 1                                         # 黑
    struct.pack_into("<I", e, 0x44, _FREESECT)          # 左兄弟
    struct.pack_into("<I", e, 0x48, _FREESECT)          # 右兄弟
    struct.pack_into("<I", e, 0x4C, _FREESECT)          # 子节点
    struct.pack_into("<I", e, 0x74, start)              # 起始扇区
    struct.pack_into("<Q", e, 0x78, size)               # 流长度
    return bytes(e)


def _cfb_build(streams: list[tuple[str, bytes]], extra_names: list[str] = ()) -> bytes:
    """搭一个能读回来的最小 OLE2/CFB 容器。

    streams     —— [(流名, 内容)]，内容会被补齐到整扇区。
    extra_names —— 只想出现在目录里、不占扇区的流名（用于模拟
                   `EncryptedPackage` / `EncryptionInfo` 这类"看名字就够"的标志流）。

    简化之处：流内容一律放主 FAT（不搭 mini 流），所以内容必须补到
    >= 4096 字节才会被当作"大流"读。本测试的夹具都满足这一点。
    """
    # 扇区规划：0 = 目录，1 = FAT，2.. = 各流的数据
    n_dir_sect = 1
    plan: list[tuple[int, int]] = []      # (起始扇区, 占用扇区数)
    cursor = 2
    blobs: list[bytes] = []
    for _name, content in streams:
        padded = content + b"\x00" * ((-len(content)) % SECT)
        plan.append((cursor, len(padded) // SECT))
        blobs.append(padded)
        cursor += len(padded) // SECT
    n_total = cursor

    # FAT
    fat = [_FREESECT] * (SECT // 4)
    fat[0] = _ENDOFCHAIN                  # 目录链：一个扇区
    fat[1] = _FATSECT                     # FAT 自身
    for start, count in plan:
        for i in range(count):
            fat[start + i] = (start + i + 1) if i < count - 1 else _ENDOFCHAIN

    # 目录项：根 + 各流 + 只登记名字的标志流
    entries = [_dir_entry("Root Entry", 5, _ENDOFCHAIN, 0)]
    for (name, content), (start, _c) in zip(streams, plan):
        entries.append(_dir_entry(name, 2, start, len(content)))
    for name in extra_names:
        entries.append(_dir_entry(name, 2, _ENDOFCHAIN, 0))
    dir_sect = b"".join(entries)
    dir_sect += b"\x00" * ((-len(dir_sect)) % SECT)

    # 头
    head = bytearray(512)
    head[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", head, 0x18, 0x003E)     # minor
    struct.pack_into("<H", head, 0x1A, 0x0003)     # major = 3
    head[0x1C:0x1E] = b"\xfe\xff"                  # 字节序标记
    struct.pack_into("<H", head, 0x1E, 9)          # 扇区 512 字节
    struct.pack_into("<H", head, 0x20, 6)          # mini 扇区 64 字节
    struct.pack_into("<I", head, 0x2C, 1)          # FAT 扇区数
    struct.pack_into("<I", head, 0x30, 0)          # 目录起始扇区
    struct.pack_into("<I", head, 0x38, 4096)       # mini 流阈值
    struct.pack_into("<I", head, 0x3C, _ENDOFCHAIN)
    struct.pack_into("<I", head, 0x40, 0)
    struct.pack_into("<I", head, 0x44, _ENDOFCHAIN)
    struct.pack_into("<I", head, 0x48, 0)
    for i in range(109):                           # DIFAT
        struct.pack_into("<I", head, 0x4C + i * 4, 1 if i == 0 else _FREESECT)

    out = bytearray(head)
    out += dir_sect                                 # 扇区 0
    out += b"".join(struct.pack("<I", v) for v in fat)   # 扇区 1
    for b in blobs:                                 # 扇区 2..
        out += b
    assert len(out) == 512 + n_total * SECT, (len(out), 512 + n_total * SECT)
    return bytes(out)


def _docx_build(text: str = "", media: bool = False) -> bytes:
    """造一个最小 docx。text 为空且 media=False 时就是"空壳文档"。"""
    body = f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" if text else "<w:p/>"
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        if media:
            z.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return buf.getvalue()


def _fib_doc(encrypted: bool) -> bytes:
    """造一段 WordDocument 流内容：合法 wIdent + 指定 fEncrypted 位。"""
    fib = bytearray(4096)
    struct.pack_into("<H", fib, 0x00, 0xA5EC)          # wIdent
    struct.pack_into("<H", fib, 0x02, 193)             # nFib
    flags = 0x0100 if encrypted else 0x0000            # bit 8 = fEncrypted
    struct.pack_into("<H", fib, 0x0A, flags)
    return bytes(fib)


# ============================================================================
#  1. 纯探测层：encryption_reason / docx_is_empty
# ============================================================================

def t_encrypted_fixtures(tmp: Path) -> None:
    from doc2md import detect

    print("\n--- 加密识别（自造 OLE2 夹具）---")

    # 1a) 加密 OOXML：OLE2 壳里只有 EncryptedPackage / EncryptionInfo
    p = tmp / "加密OOXML.docx"
    p.write_bytes(_cfb_build([], extra_names=["EncryptedPackage", "EncryptionInfo"]))
    r = detect.encryption_reason(p)
    check("加密 OOXML 被识别（EncryptedPackage 流）", bool(r), f"实得 {r!r}")

    # 1b) 加密的老式 .doc：FIB 的 fEncrypted 位为真
    p = tmp / "加密老doc.doc"
    p.write_bytes(_cfb_build([("WordDocument", _fib_doc(True))]))
    r = detect.encryption_reason(p)
    check("加密的 .doc 被识别（FIB fEncrypted）", bool(r), f"实得 {r!r}")
    check("原因里点明是 Word 文档", r is not None and "Word" in r, f"实得 {r!r}")

    # 1c) **反向**：没加密的 .doc 绝不能被误判成加密
    p = tmp / "正常doc.doc"
    p.write_bytes(_cfb_build([("WordDocument", _fib_doc(False))]))
    r = detect.encryption_reason(p)
    check("未加密的 .doc 不误报", r is None, f"实得 {r!r}")

    # 1d) 非 OLE2 文件直接返回 None
    p = tmp / "普通.txt"
    p.write_bytes(b"hello world" * 100)
    check("非 OLE2 文件返回 None", detect.encryption_reason(p) is None)

    # 1e) CFB 目录项读得出来（读目录是整个判定的基础）
    p = tmp / "带流.doc"
    p.write_bytes(_cfb_build([("WordDocument", _fib_doc(False)), ("1Table", b"\x01" * 4096)]))
    cfb = detect.CfbReader(p.read_bytes())
    names = cfb.names()
    check("CFB 能列出目录项", "Root Entry" in names and "WordDocument" in names, str(names))
    check("CFB 读回 FIB 的 wIdent", cfb.stream("WordDocument", 2) == b"\xec\xa5")


def t_empty_docx(tmp: Path) -> None:
    from doc2md import detect

    print("\n--- 空文档识别（自造 docx 夹具）---")

    p = tmp / "空壳.docx"
    p.write_bytes(_docx_build())
    check("空壳 docx 判定为空", detect.docx_is_empty(p) is True)

    p = tmp / "有文字.docx"
    p.write_bytes(_docx_build("关于安全生产工作的通知"))
    check("有正文的 docx 判定为非空", detect.docx_is_empty(p) is False)

    # 关键的反向用例：只有插图、没有文字的文档不算空
    p = tmp / "只有图.docx"
    p.write_bytes(_docx_build(media=True))
    check("只有插图的 docx 不算空（图也是内容）", detect.docx_is_empty(p) is False)

    # 这个用例守着一个只靠肉眼极难发现的坑：`<w:t` 是 `<w:tab/>`、
    # `<w:tbl>`、`<w:tc>`、`<w:tr>` 的公共前缀，用子串判断会把空文档判成有内容。
    p = tmp / "只有表格框.docx"
    p.write_bytes(_docx_build("").replace(
        b"<w:p/>", b'<w:tbl><w:tr><w:tc><w:tab/></w:tc></w:tr></w:tbl>'))
    check("<w:tab/> <w:tbl> 不被当成正文（前缀陷阱）",
          detect.docx_is_empty(p) is True,
          "若为 False 说明用了 `<w:t` 子串判断")

    p = tmp / "坏文件.docx"
    p.write_bytes(b"not a zip at all")
    check("不是 zip 时返回 None（不硬下结论）", detect.docx_is_empty(p) is None)


# ============================================================================
#  2. 端到端：引擎必须把它们记成 skipped，而不是 failed
# ============================================================================

def build_cfg(tmp: Path):
    from doc2md.config import load_config

    data = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    data["roots"] = [str(src)]
    data["output"] = {"mode": "alongside", "root": "", "layout": "mirror"}
    data["state_db"] = str(tmp / "state.db")
    data["log_dir"] = str(tmp / "logs")
    data["ocr"] = {**data.get("ocr", {}), "enabled": False}   # 不联网
    data["local_ocr"] = {**data.get("local_ocr", {}), "python_exe": ""}
    (tmp / "cfg.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return load_config(tmp / "cfg.json"), src


def t_engine_end_to_end(tmp: Path) -> None:
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print("\n--- 端到端：引擎归类（不联网、不调 COM）---")
    cfg, src = build_cfg(tmp)

    cases = {
        "空壳1.docx": _docx_build(),
        "空壳2.docx": _docx_build(),
        "加密OOXML.docx": _cfb_build([], extra_names=["EncryptedPackage", "EncryptionInfo"]),
        "加密老doc.doc": _cfb_build([("WordDocument", _fib_doc(True))]),
        "正常文档.docx": _docx_build("遂宁市船山区应急管理局关于安全生产的通知"),
    }
    for name, blob in cases.items():
        (src / name).write_bytes(blob)

    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=True, logger=print)

    # 分流要对
    planned = {p.name: eng.plan(p) for p in sorted(src.iterdir())}
    check("空壳 docx 走 empty 路线",
          all(planned[n] is not None and planned[n].route == "empty"
              for n in ("空壳1.docx", "空壳2.docx")),
          str({n: getattr(planned[n], "route", None) for n in planned}))
    check("加密 OOXML 走 encrypted 路线",
          planned["加密OOXML.docx"] is not None
          and planned["加密OOXML.docx"].route == "encrypted")
    check("加密 .doc 走 encrypted 路线",
          planned["加密老doc.doc"] is not None
          and planned["加密老doc.doc"].route == "encrypted")
    check("正常 docx 走 docx 路线",
          planned["正常文档.docx"] is not None
          and planned["正常文档.docx"].route == "docx")

    rep = eng.run(dry_run=False, limit=0)
    print(f"    结果：ok={rep.ok} skip={rep.skipped} fail={rep.failed} "
          f"blocked={rep.blocked} 跳过原因={dict(rep.skipped_by_reason)}")

    check("失败数为 0（空壳/加密都不再算失败）", rep.failed == 0, f"实得 {rep.failed}")
    check("成功 1 个（只有正常文档有内容）", rep.ok == 1, f"实得 {rep.ok}")
    check("跳过 4 个", rep.skipped == 4, f"实得 {rep.skipped}")
    check("空壳与加密进了 skipped 统计",
          any("为空" in k for k, _ in rep.skipped_by_reason.items()) and
          any("加密" in k for k, _ in rep.skipped_by_reason.items()),
          str(dict(rep.skipped_by_reason)))

    # 状态库里不能有 failed
    bad = [r for r in store._conn.execute(
        "SELECT path, status FROM files WHERE status='failed'")]
    check("状态库里没有 failed 记录", not bad, str(bad))

    # 该产出的只有正常文档那一个 md
    mds = sorted(p.name for p in src.glob("*.md"))
    check("只产出 1 个 md（正常文档）", mds == ["正常文档.md"], str(mds))
    md = (src / "正常文档.md").read_text(encoding="utf-8-sig")
    check("md 内容正确", "船山区应急管理局" in md, md[:80])

    # 重跑必须幂等：结果完全一样，不新增 md、不再报失败
    rep2 = eng.run(dry_run=False, limit=0)
    check("重跑仍无失败", rep2.failed == 0, f"实得 {rep2.failed}")
    check("重跑不新增 md", sorted(p.name for p in src.glob("*.md")) == ["正常文档.md"])

    # retry 不该把空壳/加密再拎出来（它们不是"待重试"）
    check("retry 队列里没有空壳/加密文件", store.retry_paths() == [],
          str(store.retry_paths()))

    eng.close()
    store.close()


# ============================================================================
#  3. 真实语料抽样（找不到就跳过，不算失败）
# ============================================================================

def t_real_corpus() -> None:
    """若 config.json 的 roots 里真有加密/空壳文件，顺带验一遍真实样本。"""
    from doc2md import detect

    print("\n--- 真实语料抽样（找不到就跳过）---")
    try:
        from devkit import iter_candidates

        cands = list(iter_candidates([".docx", ".doc"], max_mb=30))
    except Exception as e:  # noqa: BLE001
        print(f"  [跳过] 无法枚举语料：{type(e).__name__}: {e}")
        return

    encrypted, empty = [], []
    for p, _size in cands[:4000]:
        try:
            if detect.encryption_reason(p):
                encrypted.append(p)
                continue
            if p.suffix.lower() == ".docx" and detect.docx_is_empty(p) is True:
                empty.append(p)
        except Exception:  # noqa: BLE001
            continue
        if len(encrypted) >= 2 and len(empty) >= 3:
            break

    print(f"  扫描样本 {min(len(cands), 4000)} 个 → 加密 {len(encrypted)}、空壳 {len(empty)}")
    if not encrypted and not empty:
        print("  [跳过] 语料里没扫到加密/空壳文件（正常语料本就少见）")
        return
    for p in encrypted[:3]:
        print(f"    加密：{p.name}")
    for p in empty[:5]:
        print(f"    空壳：{p.name}")
    check("真实样本的判定都返回了原因", all(detect.encryption_reason(p) for p in encrypted))


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout

    print("=" * 74)
    print("  空文档 / 加密文件归类测试")
    print("=" * 74)

    with tempfile.TemporaryDirectory(prefix="doc2md_enc_", ignore_cleanup_errors=True) as d:
        base = Path(d)
        fix = base / "fixtures"
        fix.mkdir()
        t_encrypted_fixtures(fix)
        t_empty_docx(fix)
        t_engine_end_to_end(base / "e2e")
    t_real_corpus()

    print()
    print("=" * 74)
    print(f"  失败项：{FAILURES}")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
