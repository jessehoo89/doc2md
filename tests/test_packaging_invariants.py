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

# ---- 五、批处理类脚本必须是 CRLF -----------------------------------------------
# 为什么值得单独测：cmd 在遇到 goto/标签跳转时按**字节偏移**重新定位文件指针。
# 纯 LF 的 .bat 会让它错位到行中间，把行的**尾巴当命令执行** —— 报错长这样：
#     'uild' 不是内部或外部命令     ← 第 56 行 "echo ... build single-file installer" 的尾巴
#     'm'    不是内部或外部命令     ← 任意一行 "rem ..." 去掉 "re"
#     系统找不到指定的文件。        ← 行里带路径的碎片
# **构建仍然会成功**，所以只看结果永远发现不了，只在屏幕上刷一堆莫名其妙的报错。
# 更危险的是 installer/uninstall.bat 也会这样 —— 那是要跑到用户机器上的卸载脚本，
# 碎片里可能带上 rd / del 的片段。
#
# 关键点：`.gitattributes` 里的 `*.bat text eol=crlf` **只在 checkout 时生效**。
# 文件被工具（编辑器/脚本）重写成 LF 之后，因为 git 在比较时会做归一化，
# `git status` 是**干净的** —— 也就是说这个毛病能一路混进提交和发布包。
# 只能靠这条断言在本地拦。
print("\n[5] 批处理类脚本必须是 CRLF 行尾（纯 LF 会让 cmd 执行行尾碎片）")
NON_SOURCE_DIRS = {".git", ".venv", ".venv-gui", "build", "dist", "dist-installer",
                   "__pycache__", "_shot_out"}
SCRIPT_EXTS = (".bat", ".cmd", ".ps1")

scripts: list[tuple[Path, int, int]] = []
for _p in sorted(ROOT.rglob("*")):
    if not _p.is_file() or _p.suffix.lower() not in SCRIPT_EXTS:
        continue
    if NON_SOURCE_DIRS & set(_p.parts):
        continue
    _raw = _p.read_bytes()
    _crlf = _raw.count(b"\r\n")
    scripts.append((_p, _crlf, _raw.count(b"\n") - _crlf))

check("扫到了批处理脚本", len(scripts) >= 6, f"只找到 {len(scripts)} 个，路径判断可能错了")
_bad_crlf = [(p, crlf, lf) for p, crlf, lf in scripts if crlf == 0 and lf > 0]
check("没有纯 LF 的批处理脚本", not _bad_crlf,
      "改成 CRLF 即可；涉及：" +
      "、".join(f"{p.relative_to(ROOT)}({lf}行)" for p, _, lf in _bad_crlf))
_bad_mixed = [(p, crlf, lf) for p, crlf, lf in scripts if crlf > 0 and lf > 0]
check("没有 CRLF/LF 混用的批处理脚本", not _bad_mixed,
      "混用同样会让 cmd 定位错乱；涉及：" +
      "、".join(f"{p.relative_to(ROOT)}(CRLF={c},LF={l})" for p, c, l in _bad_mixed))

# ---- 六、卸载程序必须真的进载荷 -----------------------------------------------
# 这条属于"漏了不报错、但用户装完发现卸不掉"的类型：注册表里的 UninstallString 指向
# `<安装目录>\uninstall.exe`，载荷里少一个文件它就是个死链，而安装过程**不会**报错。
print("\n[6] 卸载程序（uninstall.exe）必须被打包编排带上")
check("installer/uninstaller.spec 存在",
      (ROOT / "installer" / "uninstaller.spec").is_file())
check("installer/uninstall_app.py 存在",
      (ROOT / "installer" / "uninstall_app.py").is_file())
check("make_installer.py 定义了卸载程序构建步骤",
      "def build_uninstaller" in maker and "UNINSTALLER_SPEC" in maker)
check("编排里真的会调用它（不是只定义）",
      re.search(r"^\s*build_uninstaller\(\)", maker, re.M) is not None,
      "只定义不调用，载荷里就没有 uninstall.exe")
check("载荷里带上 uninstall.exe 这个名字",
      'UNINSTALLER_IN_PAYLOAD = "uninstall.exe"' in maker)
check("载荷缺卸载程序时直接中止（而不是打个警告继续走）",
      bool(re.search(r"die\(f\"载荷缺少卸载程序", maker)))

# 卸载程序的编码守卫：它也有控制台文案（/S 模式），且**不能** import doc2md
# （卸载时 _internal 正在被删，不能依赖被卸载的那套代码）。
uapp = read(ROOT / "installer" / "uninstall_app.py")
check("卸载程序自带 stdio 守卫（不 import doc2md）",
      "def safe_stdio" in uapp and "from doc2md" not in uapp,
      "卸载时不能依赖正在被删的 doc2md 包")
check("卸载程序的输出通道会调用 safe_stdio",
      re.search(r"def setup_output.*?safe_stdio\(\)", uapp, re.S) is not None)

# --------------------------------------------------------------------------- #
print("\n" + "=" * 74)
print(f"通过 {PASS}　失败 {len(FAILS)}")
if FAILS:
    for f in FAILS:
        print(f"  ✗ {f}")
    sys.exit(1)
print("全部通过。")
