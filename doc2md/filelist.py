"""转换清单的读取与展开（`doc2md convert` 的输入层）。

为什么需要这一层：`run` 只能整目录跑，而实际工作中经常是"我只想转这批文件"
（比如按磁盘现状补转某几百份、或转别处拷来的一个清单）。把清单直接丢给
`run` 行不通 —— 它只认 `cfg.roots`。

本模块只做一件事：把「命令行上的路径 + 清单文件 + 目录」统一成一批**确实存在
且能转换**的文件，并**如实报告**哪些没找到、哪些不在转换范围内。

⚠ 缺失项必须显式报出来：清单里写错一个字符不会抛任何异常，只会安静地少转
一份文件 —— 等发现的时候 md 已经入库了。宁可多打几行提示。

清单文件的容错（全都来自实际会遇到的情况）：
  * 编码 —— `dir /b > list.txt` 出来的是 GBK，手工另存的常带 UTF-8 BOM，
    从别处拷来的可能是 UTF-16。按 BOM 判断，无 BOM 时先试 UTF-8 再退 GBK。
  * 每行一个路径；`#` 开头的整行是注释；空行忽略。
  * 行首尾空白、以及从 Excel 复制来的 Tab 分隔行（只取第一列）都会吃掉。
  * 路径可以带引号（资源管理器"复制为路径"给的就是带引号的）。
  * 相对路径先相对当前目录找，找不到再相对**清单文件所在目录**找 ——
    清单和文件放一起是最自然的用法。
  * 目录行按配置的扩展名递归展开（"这个文件夹里的都要"）。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileList:
    """清单解析结果。各字段都如实记录，不做静默丢弃。"""

    paths: list[Path] = field(default_factory=list)   # 去重后、确实存在、可转换
    missing: list[str] = field(default_factory=list)  # 找不到的条目（原样保留）
    unsupported: list[Path] = field(default_factory=list)  # 存在但扩展名不在范围内
    from_dirs: int = 0        # 由目录行展开出来的文件数
    duplicates: int = 0       # 重复出现的条数
    source: str = ""          # 来源描述（清单文件路径 / 命令行 / 标准输入）
    encoding: str = ""        # 读清单文件时实际使用的编码

    @property
    def ok(self) -> bool:
        """是否至少解析出了一个可转换的文件。"""
        return bool(self.paths)

    def summary(self) -> str:
        """一行摘要，供 CLI 打印。"""
        bits = [f"{len(self.paths)} 个文件"]
        if self.from_dirs:
            bits.append(f"目录展开 {self.from_dirs}")
        if self.duplicates:
            bits.append(f"去重 {self.duplicates}")
        if self.unsupported:
            bits.append(f"格式不支持 {len(self.unsupported)}")
        if self.missing:
            bits.append(f"找不到 {len(self.missing)}")
        return "，".join(bits)


# --------------------------------------------------------------------------- #
#  读取清单文件
# --------------------------------------------------------------------------- #

def _decode(raw: bytes) -> tuple[str, str]:
    """把清单文件的字节解成文本，返回 (文本, 实际使用的编码名)。

    先按 BOM 判（有 BOM 就不该猜），无 BOM 时**先试 UTF-8 再退 GBK** ——
    GBK 的解码空间很大、几乎不会抛错（UTF-8 中文用 GBK 解会得到乱码但"成功"），
    反过来 UTF-8 解 GBK 中文则多半直接失败，所以这个顺序是可靠的。
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    # 两种都不行（混进了非法字节）——用替换字符保底，至少不丢整份清单
    return raw.decode("gbk", errors="replace"), "gbk(有非法字节)"


def read_manifest(source: str) -> tuple[list[str], str]:
    """读清单来源，返回 (原始行列表, 编码名)。

    source 为 `-` 时读标准输入 —— 这样 `dir /b /s *.pdf | doc2md convert --list -`
    这类管道用法也能work，不必先落一个临时文件。
    """
    if source == "-":
        raw = sys.stdin.buffer.read()
        text, enc = _decode(raw)
        return text.splitlines(), enc or "-"
    p = Path(source).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"清单文件不存在：{p}")
    text, enc = _decode(p.read_bytes())
    return text.splitlines(), enc


# --------------------------------------------------------------------------- #
#  条目清洗与展开
# --------------------------------------------------------------------------- #

def clean_entry(line: str) -> str:
    """把清单里的一行洗成路径字符串；返回空串表示这行该忽略。"""
    s = line.strip()
    if not s or s.startswith("#"):
        return ""
    if "\t" in s:                       # 从 Excel / 表格粘贴来的，只取第一列
        s = s.split("\t", 1)[0].strip()
    # 资源管理器"复制为路径"会带引号；成对才剥，避免误伤合法的引号字符
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _resolve(s: str, base: Path) -> Path | None:
    """把条目解析成一个存在的路径（文件或目录）；找不到返回 None。"""
    cand = Path(s).expanduser()
    if cand.is_absolute():
        return cand if cand.exists() else None
    for b in (Path.cwd(), base):
        p = b / cand
        if p.exists():
            return p
    return None


def _iter_dir(root: Path, cfg) -> list[Path]:
    """展开目录行。复用 engine 里那份过滤规则，避免两处各写一遍而漂移。"""
    from .engine import iter_docs

    return list(iter_docs(root, cfg))


def collect(entries, *, cfg=None, base_dir: str | Path | None = None,
            source: str = "命令行") -> FileList:
    """把一批条目解析成可转换的文件清单。

    cfg 提供扩展名白名单（`watch_ext_set`）与排除目录规则；为 None 时只做
    "存在性 + 目录展开（按扩展名粗筛）"，供不加载配置的场合（测试）使用。
    """
    base = Path(base_dir) if base_dir else Path.cwd()
    res = FileList(source=source)
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p).lower()            # Windows 路径大小写不敏感，按小写去重
        if key in seen:
            res.duplicates += 1
            return
        seen.add(key)
        res.paths.append(p)

    for raw in entries:
        s = clean_entry(raw)
        if not s:
            continue
        p = _resolve(s, base)
        if p is None:
            res.missing.append(s)
            continue
        if p.is_dir():
            found = _iter_dir(p, cfg) if cfg is not None else _fallback_dir(p)
            before = len(res.paths)
            for f in found:
                add(f)
            res.from_dirs += len(res.paths) - before
            continue
        if _routeable(p, cfg):
            add(p)
        else:
            res.unsupported.append(p)

    return res


def _fallback_dir(root: Path) -> list[Path]:
    """没有配置时的目录展开：只做最基本的过滤，供测试/独立使用。"""
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() not in (".md", "") \
                and not p.name.startswith("~$"):
            out.append(p)
    return out


def _routeable(p: Path, cfg) -> bool:
    """文件扩展名是否在可转换范围内。没有配置时一律放行（交给 plan 判断）。"""
    ext = p.suffix.lower()
    if ext in (".md", ""):
        return False
    if cfg is None:
        return True
    return ext in cfg.watch_ext_set
