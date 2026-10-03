#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安装程序的提权与参数解析测试（离线，不弹 UAC、不改系统）。

为什么单独测这一块：这里踩过一个不好查的坑。

    最初安装程序用的是 asInvoker 清单 + 运行时 `ShellExecuteW("runas")` 自我提权，
    想法是"只在真写不进去时才弹 UAC"。结果真实双击时表现为**调不出 UAC、一直卡在
    那里** —— 提权被推迟到点「开始安装」之后，那一步没成就静默卡死，没有任何提示。
    用户只能自己想到"右键以管理员身份运行"。

    改成清单里写 requireAdministrator 之后，Windows 在进程启动时就弹 UAC，行为
    可预期。于是有两条恒定约束，用测试钉住：

    1. 安装程序 exe **必须**带 requireAdministrator；
    2. 被安装的 doc2md 本身**必须不带** —— 它是日常工具，每次启动都弹 UAC 没人受得了。

用法（用哪个解释器都行，纯标准库）：
    .venv\\Scripts\\python.exe tests\\test_installer_logic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))

import installer_app as IA  # noqa: E402

DIST_INSTALLER = ROOT / "dist-installer"
DIST_APP = ROOT / "dist" / "doc2md"
SETUP_EXE = DIST_INSTALLER / "doc2md-安装程序.exe"

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


def has_marker(path: Path, marker: bytes, chunk_mb: int = 8) -> bool:
    """分块在文件里找字节串（84MB 的包别整块读进内存）。"""
    if not path.is_file():
        return False
    size = chunk_mb * 1024 * 1024
    tail = b""
    with path.open("rb") as f:
        while True:
            buf = f.read(size)
            if not buf:
                return False
            if marker in tail + buf:
                return True
            tail = buf[-len(marker):]


# --------------------------------------------------------------------------- #
print("=" * 74)
print("  安装程序：提权判断与参数解析")
print("=" * 74)

print("\n[1] 受保护目录识别（决定装完要不要补 Users 写权限）")
check("C:\\Program Files\\doc2md 算受保护",
      IA._under_protected_root(Path(r"C:\Program Files\doc2md")))
check("C:\\Program Files (x86)\\x 算受保护",
      IA._under_protected_root(Path(r"C:\Program Files (x86)\x")))
check("Windows 目录算受保护",
      IA._under_protected_root(Path(str(IA._PROTECTED_ROOTS[2])) / "System32" / "x"))
check("D:\\doc2md 不算受保护（不该额外放宽权限）",
      not IA._under_protected_root(Path(r"D:\doc2md")))
check("C:\\ProgramData\\doc2md 不算受保护",
      not IA._under_protected_root(Path(r"C:\ProgramData\doc2md")))
# 前缀相近但不同层级，不能被误判
check("C:\\Program FilesExtra 不算受保护（防前缀误判）",
      not IA._under_protected_root(Path(r"C:\Program FilesExtra")))

print("\n[2] 命令行参数解析")
opt, dest = IA.parse_args([])
check("无参数：进图形界面（silent=False）", opt["silent"] is False, str(opt))
check("无参数：默认装到 Program Files 下的 doc2md",
      dest == IA.DEFAULT_DIR and dest.name == "doc2md", str(dest))
check("无参数：默认建快捷方式", opt["shortcuts"] is True)

opt, dest = IA.parse_args(["/S", "/D=D:\\doc2md", "/NOICONS"])
check("/S 识别为静默", opt["silent"] is True, str(opt))
check("/D= 指定目录生效", dest == Path(r"D:\doc2md"), str(dest))
check("/NOICONS 关闭快捷方式", opt["shortcuts"] is False, str(opt))

opt, dest = IA.parse_args(["--dir", r"E:\tools\doc2md"])
check("--dir 空格形式也认", dest == Path(r"E:\tools\doc2md"), str(dest))

opt, _ = IA.parse_args(["--help"])
check("--help 识别", opt["help"] is True)
opt, _ = IA.parse_args(["/?"])
check("/? 也当帮助", opt["help"] is True)
opt, _ = IA.parse_args(["/SiLeNt"])
check("参数大小写不敏感", opt["silent"] is True)
opt, _ = IA.parse_args(["/NORESTART"])
check("/NORESTART 被吃掉且不报错", opt["silent"] is False)

print("\n[3] 提权清单（这是本次修复的核心）")
if SETUP_EXE.is_file():
    check("安装程序 exe 带 requireAdministrator（双击即弹 UAC）",
          has_marker(SETUP_EXE, b"requireAdministrator"),
          f"未在 {SETUP_EXE.name} 里找到该标记；双击会不弹 UAC 而静默失败")
    check("安装程序 exe 带 asInvoker 之外的提权请求（反向确认不是没写清单）",
          not has_marker(SETUP_EXE, b"level=\"asInvoker\""),
          "残留 asInvoker：清单可能没生效")
    mb = SETUP_EXE.stat().st_size / 1024 / 1024
    print(f"  [信息] {SETUP_EXE.name}：{mb:.1f} MB")
else:
    print(f"  [跳过] 还没构建安装程序（{SETUP_EXE}），构建后请重跑本测试")

for name in ("doc2md.exe", "doc2md-gui.exe"):
    p = DIST_APP / name
    if not p.is_file():
        print(f"  [跳过] {name} 尚未构建")
        continue
    check(f"{name} 不带 requireAdministrator（日常工具不该每次弹 UAC）",
          not has_marker(p, b"requireAdministrator"),
          "被安装的程序要求提权了：普通用户双击会一直被 UAC 拦")

print("\n[4] 卸载脚本的提权方式")
uni = ROOT / "installer" / "uninstall.bat"
check("uninstall.bat 存在", uni.is_file())
if uni.is_file():
    text = uni.read_text(encoding="utf-8", errors="replace")
    check("uninstall.bat 会自我提权（Program Files 下删除需要管理员）",
          "-Verb RunAs" in text)
    check("uninstall.bat 正文纯 ASCII（cmd 代码页安全）",
          all(ord(c) < 128 for c in text),
          "含非 ASCII 字符，双击可能乱码")

print()
print("=" * 74)
if FAILS:
    print(f"失败项：{len(FAILS)}")
    for f in FAILS:
        print("  - " + f)
    print("=" * 74)
    sys.exit(1)

print(f"全部通过（{PASS} 项）")
print("=" * 74)
sys.exit(0)
