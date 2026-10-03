#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""doc2md/pdf_zhdoc.py —— vendor 上游接入层的约束测试。

分两半：
  * 静态断言：vendor 子项目必须完整、上游那几处修正不能被回滚、打包配置必须带上它；
  * 纯逻辑：跨行标题合并 / 标题正文同行切分 / 版头降级 / 渲染层级，都不需要真实 PDF。

为什么值得测：这几条都属于「悄悄回滚就出事、但出事不报错」的类型 ——
比如 `_ORG_TAIL` 拿掉，红头机关名就会和标题粘成一行；`pathex` 漏掉 vendor，
打出来的包一跑 rule 档就 ImportError，而源码态完全正常。

用法（纯标准库，离线）：
    .venv\\Scripts\\python.exe tests\\test_pdf_zhdoc.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VENDOR = ROOT / "vendor" / "ZhDocParser"
PDF_EXTRACTOR = VENDOR / "zhdocparser" / "extractors" / "pdf_extractor.py"
VENDOR_INIT = VENDOR / "zhdocparser" / "__init__.py"

PASS = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAILS.append(f"{name}｜{detail}")
        print(f"  [失败] {name}　{detail}")
    return bool(cond)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
print("=" * 74)
print("  vendor 上游接入层（ZhDocParser）")
print("=" * 74)

# ---- 一、vendor 子项目必须完整 ------------------------------------------------
print("\n[1] vendor 子项目完整（少了文件就没法跟上游对齐 / PR）")
check("vendor/ZhDocParser 目录存在", VENDOR.is_dir())
check("pdf_extractor.py 存在", PDF_EXTRACTOR.is_file())
check("LICENSE 存在（MIT 要求保留）", (VENDOR / "LICENSE").is_file())
check("zhdocparser/__init__.py 存在", VENDOR_INIT.is_file())
gitignore = read(ROOT / ".gitignore")
check(".gitignore 放行 vendor/**（否则上万个 *.md 规则会吃掉上游文档）",
      "!vendor/**" in gitignore,
      "缺了它 vendor 下的 .md 不会被提交，更新/PR 都会对不齐")

# ---- 二、上游修正不能被回滚 ---------------------------------------------------
print("\n[2] 上游四处修正必须还在（都是可单独 PR 回去的通用缺陷）")
init_src = read(VENDOR_INIT)
# 只看**顶层**语句（`__getattr__` 里那个缩进的 `from zhdocparser import sdk` 是有意为之）
init_top = "\n".join(ln for ln in init_src.split("\n") if ln and not ln[0].isspace())
check("惰性导入：顶层不再 import sdk",
      "sdk" not in init_top,
      "顶层导入会让任何子模块导入都拖进 python-docx/lxml")
check("惰性导入：改用 PEP 562 __getattr__", "def __getattr__" in init_src)

src = read(PDF_EXTRACTOR)
check("改用 import pymupdf as fitz",
      bool(re.search(r"^import pymupdf as fitz$", src, re.M)),
      "PyMuPDF 1.28 起 import fitz 会打 deprecation 警告，未来会移除")
check("不再直接 import fitz",
      not bool(re.search(r"^import fitz$", src, re.M)))

check("坐标归一：用了 page.rotation_matrix",
      "rotation_matrix" in src,
      "get_text() 返回未旋转坐标、page.rect 是旋转后的，混用会让分栏判断全错")

# 编号正则顺序：三级必须排在 `\\d+[、.]` 之前
m3 = re.search(r'r"\^\\d\+\\\.\\d\+\\\.\\d\+', src)
m2 = re.search(r'r"\^\\d\+\[、\.\]', src)
check("编号正则顺序修正：`\\d+.\\d+.\\d+` 排在 `\\d+[、.]` 之前",
      bool(m3) and bool(m2) and m3.start() < m2.start(),
      "原顺序会让 1.1.1 被前缀 1. 抢先匹配、降级成二级")
check("小数防护：(?!\\d) 已加上", "(?!\\d)" in src,
      "否则 `1.5 倍…` 这类正文会被误判成编号标题")

# ---- 三、打包配置必须带上 vendor ----------------------------------------------
print("\n[3] 打包配置（少一行，exe 跑到 rule 档就 ImportError）")
spec = read(ROOT / "doc2md.spec")
check("spec 把 vendor 目录加进 pathex",
      bool(re.search(r"pathex\.append\(\s*str\(\s*VENDOR_ZH", spec)),
      "vendor 不在默认搜索路径上")
check("spec 显式列了 zhdocparser.extractors.pdf_extractor",
      "zhdocparser.extractors.pdf_extractor" in spec,
      "接入层是在函数体内 import 的，静态分析看不到")
check("spec 没把 zhdocparser 排进 excludes",
      not re.search(r"excludes\s*=\s*\[[^\]]*zhdocparser", spec, re.S))
check("spec 列了 pydantic 链路（schemas 依赖它）",
      "zhdocparser.schemas" in spec)

# ---- 四、纯逻辑：切分 / 降级 / 渲染层级 ---------------------------------------
print("\n[4] 标题正文同行切分")
from doc2md.pdf_zhdoc import (  # noqa: E402
    _ADDRESSEE,
    _BARE_NUMBER,
    _MASTHEAD,
    _ORG_TAIL,
    _SPLIT_HEAD_BODY,
    _normalize_sections,
    _render_level,
)

m = _SPLIT_HEAD_BODY.match("（一）整合职能组建监管执法机构。将法律法规赋予县级应急管理部门的有关危险化学品")
check("切出标题 + 正文", bool(m) and m.group(1) == "（一）整合职能组建监管执法机构",
      f"实际：{m.groups() if m else None}")
m2 = _SPLIT_HEAD_BODY.match("（二）厘清层级分工健全监管执法体系。按照健全完善应急")
check("正文较短时也切（阈值已放宽）", bool(m2) and m2.group(1) == "（二）厘清层级分工健全监管执法体系")
check("标题无句号 → 不切", _SPLIT_HEAD_BODY.match("（一）加强领导") is None)
check("无编号的普通句子 → 不切",
      _SPLIT_HEAD_BODY.match("经区委、区政府同意，现将《某某方案》印发给你们。请认真贯彻") is None)

print("\n[5] 版头元信息 / 发文机关识别")
check("文号「办〔2022〕-10」被判为版头", any(r.match("办〔2022〕-10") for r in _MASTHEAD))
check("文号「遂船府办发〔2023〕15号」被判为版头",
      any(r.match("遂船府办发〔2023〕15号") for r in _MASTHEAD))
check("成文日期被判为版头", any(r.match("2026年10月3日") for r in _MASTHEAD))
check("正常标题不误判", not any(r.match("关于印发《某某方案》的通知") for r in _MASTHEAD))
check("主送机关（以冒号收尾）", bool(_ADDRESSEE.match("各乡镇党委，各街道、园区党工委，各乡镇人民政府：")))
check("发文机关后缀被识别", bool(_ORG_TAIL.search("中共遂宁市船山区委办公室")))
check("公文标题不会被当成发文机关", not _ORG_TAIL.search("关于印发《某某方案》的通知"))

print("\n[6] 渲染层级：编号型下沉一级")
check("`一、总体要求` → h2（给文档大标题让位）", _render_level(1, "一、总体要求") == 2)
check("`关于印发《…》的通知` → h1", _render_level(1, "关于印发《某某方案》的通知") == 1)
check("`（一）整合职能…` → h3", _render_level(2, "（一）整合职能组建监管执法机构") == 3)
check("正文 level 0 保持 0", _render_level(0, "") == 0)

print("\n[7] _normalize_sections：合并 / 切分 / 降级")


def _sec(heading, level=1, content="", page=1, size=23.0):
    return SimpleNamespace(
        heading=heading, level=level, content=content,
        page_number=page, page_end=page,
        metadata={"font_size": size},
    )


doc = SimpleNamespace(sections=[
    _sec("中共遂宁市船山区委办公室", size=44.9),          # 红头：字号大
    _sec("中共遂宁市船山区胡公室遂宁市船山区人民政府办公室"),   # 发文机关
    _sec("关于印发《遂宁市船山区深化应急管理综合"),           # 标题上半
    _sec("行政执法改革实施方案》的通知"),                    # 标题下半 → 应与上半合并
    _sec("办〔2022〕-10", size=20.0),                    # 文号 → 降级
    _sec("一、总体要求", content="以习近平新时代……"),
])
items = _normalize_sections(doc)
heads = [it[1] for it in items if it[1]]
check("红头单独成条（不粘标题）", "中共遂宁市船山区委办公室" in heads)
check("发文机关没被粘进标题",
      not any("人民政府办公室关于印发" in h for h in heads),
      f"实际：{heads}")
check("跨行标题已合并成一整句",
      "关于印发《遂宁市船山区深化应急管理综合行政执法改革实施方案》的通知" in heads)
check("文号被降级为正文（不再当标题）", "办〔2022〕-10" not in heads)
check("`一、总体要求` 仍是标题", "一、总体要求" in heads)
check("标题总数符合预期（红头/发文机关/合并后标题/一、 共 4）", len(heads) == 4,
      f"实际 {len(heads)}：{heads}")

# 标题正文同行：切开后正文要变成该节的第一行
doc2 = SimpleNamespace(sections=[
    _sec("（一）整合职能组建监管执法机构", level=2,
         content="将法律法规赋予县级应急管理部门的有关职能进行整合。"),
])
it = _normalize_sections(doc2)[0]
check("切分后正文落到 lines 首行", it[2] and it[2][0].startswith("将法律法规"),
      f"实际 {it[2]}")

# 字号差很大 → 不合并
doc3 = SimpleNamespace(sections=[
    _sec("中共遂宁市船山区委办公室", size=44.9),
    _sec("关于印发《某某方案》的通知", size=23.0),
])
heads3 = [it[1] for it in _normalize_sections(doc3) if it[1]]
check("字号差 >1pt 时不合并", len(heads3) == 2, f"实际 {heads3}")

print("\n[8] 纯编号行与标题被拆成两行（中文标准常见）+ 标准版头")
check("`3.2` 被认成纯编号行", bool(_BARE_NUMBER.match("3.2")))
check("`一、` 被认成纯编号行", bool(_BARE_NUMBER.match("一、")))
check("`（1）` 被认成纯编号行", bool(_BARE_NUMBER.match("（1）")))
check("`3.2 阻隔防爆材料` 不是纯编号行",
      not _BARE_NUMBER.match("3.2 阻隔防爆材料"),
      "带正文的行不能被当成纯编号，否则会把它和下一行错误粘在一起")
check("标准发布日期 `2005-04-13发布` 判为版头",
      any(r.match("2005-04-13发布") for r in _MASTHEAD))
check("标准实施日期 `2005-10-01实施` 判为版头",
      any(r.match("2005-10-01实施") for r in _MASTHEAD))
check("ICS 分类号判为版头", any(r.match("ICS 13.230") for r in _MASTHEAD))

doc4 = SimpleNamespace(sections=[
    _sec("3.2", level=2, size=11.0),
    _sec("阻隔防爆材料separatematerial", level=3, size=11.0),
])
heads4 = [it[1] for it in _normalize_sections(doc4) if it[1]]
check("`3.2` 与它的标题已合并回一行",
      heads4 == ["3.2 阻隔防爆材料separatematerial"],
      f"实际 {heads4}（不合并会得到一串 `### 3.2` 空标题、标题数虚高数倍）")

doc5 = SimpleNamespace(sections=[
    _sec("7.1 一般要求", level=2, size=11.0),
    _sec("单独的一段正文内容。", level=0, size=11.0, content="后面还有正文。"),
])
heads5 = [it[1] for it in _normalize_sections(doc5) if it[1]]
check("带正文的编号标题不会被误合并", "7.1 一般要求" in heads5, f"实际 {heads5}")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 74)
print(f"通过 {PASS}　失败 {len(FAILS)}")
if FAILS:
    for f in FAILS:
        print(f"  ✗ {f}")
    sys.exit(1)
print("全部通过。")
