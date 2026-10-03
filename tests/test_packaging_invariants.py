#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包恒定约束：用静态断言钉住两个「一删就整类文档全灭」的点。

为什么值得单独测：这两处都属于**删一行、注释掉一行就出事，但出事不报错**的类型，
而且症状离原因很远。

    一、模型数据文件
    `pymupdf-layout` 是独立发行包，把版面模型塞进 `pymupdf/layout/`。PyInstaller
    按 `pymupdf` 自己的清单收集，天然看不见它们。少了
    `collect_data_files("pymupdf", subdir="layout")` 这一行，打出来的包看着一切正常，
    但 `import pymupdf4llm` 当场炸 —— **所有带文字层的 PDF 全部转不出来**。
    实测：2026-10-03 全量 3809 个文件里 292 个失败，正好是 pdf_text 一整类。

    二、控制台编码
    Windows 控制台是 GBK，文案里有 `↳ ⚠ ⭐ ✓ ✗ ⊘ ↔` 这些编不出的符号。少调一次
    `make_stdio_safe()`，报错信息本身就会抛 UnicodeEncodeError，把原始异常盖掉，
    屏幕上只剩 "Failed to execute script"。

用法（纯标准库，离线）：
    .venv\\Scripts\\python.exe tests\\test_packaging_invariants.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEC = ROOT / "doc2md.spec"
MAKER = ROOT / "make_installer.py"

# 必须在**第一时间**装上编码守卫的入口（新增入口要加进来）
ENTRY_POINTS = ["app.py", "launcher.py", "doc2md/cli.py", "gui.py"]

PASS = 0
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS
    if ok:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAILS.append(f"{name}｜{detail}")
        print(f"  [失败] {name}　{detail}")
    return ok


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
print("=" * 74)
print("  打包恒定约束（模型数据文件 / 入口编码守卫）")
print("=" * 74)

# ---- 一、版面模型必须被打进包 -------------------------------------------------
print("\n[1] doc2md.spec 必须收集 pymupdf/layout 的数据文件（49MB 版面模型）")
spec = read(SPEC)
check("spec 里调用了 collect_data_files",
      "collect_data_files" in spec, "连这个函数都没导入")
check("收集范围是 pymupdf 的 layout 子目录",
      bool(re.search(r'collect_data_files\(\s*["\']pymupdf["\']\s*,\s*subdir\s*=\s*["\']layout["\']\s*\)', spec)),
      '应形如 collect_data_files("pymupdf", subdir="layout")')
check('spec 里没把 pymupdf/onnxruntime/numpy 塞进 excludes',
      not re.search(r'excludes\s*=\s*\[[^\]]*(onnxruntime|numpy)', spec, re.S),
      "排除它们会让所有文本层 PDF 转不出来")
check("spec 里没排除 tkinter（窗口版会启动即崩）",
      not re.search(r'excludes\s*=\s*\[[^\]]*["\']tkinter["\']', spec, re.S))

# ---- 二、四个入口都要装编码守卫 -----------------------------------------------
print("\n[2] 每个入口都必须调 make_stdio_safe()")
for rel in ENTRY_POINTS:
    p = ROOT / rel
    if not p.is_file():
        check(f"{rel} 存在", False, "文件都没了")
        continue
    src = read(p)
    check(f"{rel} 调用了 make_stdio_safe()",
          "make_stdio_safe()" in src, "没装守卫，报错信息可能把进程搞崩")

# 守卫本身要能被 import 且幂等、容错
print("\n[3] stdio 守卫的行为")
from doc2md.stdio import make_stdio_safe   # noqa: E402

check("可重复调用不报错", (make_stdio_safe(), make_stdio_safe(), True)[-1])

# 注意：断言期间要临时把 sys.stdout 换掉，而**此时 print 是静默 no-op**
# （CPython 故意如此，便于用 sys.stdout = None 关掉输出）。所以先记结论、
# 恢复 stdout 之后再打印，否则测试结果会被自己的重定向吞掉。
_saved = sys.stdout


def _probe(replacement) -> bool:
    """把 sys.stdout 换成 replacement 后调一次守卫，无论成败都复原。"""
    try:
        sys.stdout = replacement
        make_stdio_safe()
        return True
    except Exception:                       # noqa: BLE001
        return False
    finally:
        sys.stdout = _saved


class _Dummy:                               # 不认 reconfigure 的哑对象
    def write(self, s):  # noqa: ANN001, D401
        return len(s)


check("stdout 为 None 时容错（窗口版就是这样）", _probe(None))
check("遇到不认 reconfigure 的哑流也不报错", _probe(_Dummy()))
check("探测完 stdout 已复原", sys.stdout is _saved)

# ---- 三、构建脚本不能"先删后建" -----------------------------------------------
print("\n[4] make_installer.py 用改名挪开，而不是 rmtree（本机有 SAFE_DELETE 防护）")
maker = read(MAKER)
check("定义了 _shelve_dir", "def _shelve_dir" in maker)
check("打包前挪开 dist/doc2md",
      bool(re.search(r'_shelve_dir\(DIST_APP', maker)), "PyInstaller 清目录会被防护拦下")
check("打包前挪开 PyInstaller workpath",
      bool(re.search(r'_shelve_dir\(workpath', maker)), "同上")
check("没有对 dist/doc2md 直接 rmtree",
      "rmtree(DIST_APP" not in maker, "会撞上批量删除防护、构建中断")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 74)
print(f"通过 {PASS}　失败 {len(FAILS)}")
if FAILS:
    for f in FAILS:
        print(f"  ✗ {f}")
    sys.exit(1)
print("全部通过。")
