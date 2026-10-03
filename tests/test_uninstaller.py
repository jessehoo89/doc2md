#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""卸载程序的离线测试。

**不碰真机的东西**：快捷方式与注册表这两步在测试里被换成哑实现 —— 跑一次测试就把
用户桌面上的快捷方式删掉、把注册表里的卸载项删掉，那是不能接受的。

真正验证的是「删除安装目录」这段：在临时沙箱里造一棵跟安装目录同构的树
（两个 exe + `_internal\\` 一堆文件 + 配置 + 日志），跑完整流程，断言整个目录确实没了。
这棵树对应真实场景的一个硬约束 —— 卸载程序**自己躺在被删的目录里**，Windows 不允许
删除正在运行的程序文件，所以正常路径是「先把自己复制到 %TEMP% 再动手」；
本测试同时覆盖"交接成功"（自己不在目录里）和"交接失败"（自己就在目录里，
只能删掉其他所有东西并把残留如实报出来）两种情形。

用法（纯标准库，离线）：
    .venv\\Scripts\\python.exe tests\\test_uninstaller.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))

import uninstall_app as UA  # noqa: E402

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


def make_fake_install(root: Path) -> None:
    """造一棵跟真实安装目录同构的树。"""
    (root / "_internal" / "pymupdf" / "layout").mkdir(parents=True, exist_ok=True)
    files = {
        "doc2md.exe": b"MZ fake",
        "doc2md-gui.exe": b"MZ fake",
        "uninstall.exe": b"MZ fake",
        "uninstall.bat": b"@echo off\r\n",
        "README.md": b"# readme",
        "LICENSE": b"MIT",
        "config.json": b"{}",
        "config.example.json": b"{}",
        ".env": b"",
        ".env.example": b"",
        "state.db": b"SQLite format 3\x00",
        "logs/run.log": b"log",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for i in range(40):
        (root / "_internal" / "pymupdf" / "layout" / f"m{i}.onnx").write_bytes(b"x" * 32)


def stub_side_effects() -> list[str]:
    """把快捷方式/注册表/结束进程换成哑实现，返回原来的函数名清单。"""
    UA.stop_running = lambda: []                       # 别真去 taskkill 用户的项目
    UA.remove_shortcuts = lambda: []                   # 别真删桌面快捷方式
    UA.remove_registry = lambda: False                 # 别真删注册表
    return ["stop_running", "remove_shortcuts", "remove_registry"]


# --------------------------------------------------------------------------- #
print("=" * 74)
print("  卸载程序：参数解析 / 交接判断 / 沙箱删除")
print("=" * 74)

print("\n[1] 参数解析")
opt = UA.parse_args([])
check("无参数：进图形界面", opt["silent"] is False, str(opt))
check("无参数：不是 %TEMP% 副本", opt["from_temp"] is False)
opt = UA.parse_args(["/S"])
check("/S 静默", opt["silent"] is True)
opt = UA.parse_args(["/s"])
check("小写 /s 也认", opt["silent"] is True)
opt = UA.parse_args(["/FROM-TEMP"])
check("/FROM-TEMP 识别为副本模式", opt["from_temp"] is True)
opt = UA.parse_args(["/D=D:\\doc2md"])
check("/D= 指定目录", opt["dir"] == r"D:\doc2md", str(opt["dir"]))
opt = UA.parse_args(["--dir", r"E:\tools\doc2md"])
check("--dir 空格形式也认", opt["dir"] == r"E:\tools\doc2md")
opt = UA.parse_args(["/?"])
check("/? 当帮助", opt["help"] is True)
opt = UA.parse_args(["/SiLeNt"])
check("参数大小写不敏感", opt["silent"] is True)

print("\n[2] 目标目录推断")
check("源码方式下不猜目录（防误删），要求显式 /D=",
      UA.resolve_target(UA.parse_args([])) is None,
      "源码运行时默认目录很容易算错，必须强制显式指定")
check("源码方式下给了 /D= 就用它",
      UA.resolve_target(UA.parse_args(["/D=D:\\x"])) == Path(r"D:\x"))

print("\n[3] is_inside（决定要不要把自己搬到 %TEMP%）")
check("子路径算在内部", UA.is_inside(Path(r"D:\a\b\c.exe"), Path(r"D:\a")))
check("同目录算在内部", UA.is_inside(Path(r"D:\a\u.exe"), Path(r"D:\a")))
check("父目录不算", not UA.is_inside(Path(r"D:\a"), Path(r"D:\a\b")))
check("同名前缀不算（防字符串前缀误判）",
      not UA.is_inside(Path(r"D:\abc\u.exe"), Path(r"D:\a")))

print("\n[4] hand_off：源码方式 / 不在目标目录里 都不搬")
check("源码方式不搬（脚本文件没被锁）",
      UA.hand_off(Path(tempfile.gettempdir()), silent=True) is False)
_orig_dev = UA.is_dev
UA.is_dev = lambda: False          # 假装已经冻结成 exe
try:
    check("exe 不在目标目录里时不搬（别的地方运行不受影响）",
          UA.hand_off(Path(r"D:\nonexistent-target-xyz"), silent=False) is False)
finally:
    UA.is_dev = _orig_dev

print("\n[5] 沙箱卸载：自己不在目录里 → 目录应被完全删除")
stub_side_effects()
sandbox = Path(tempfile.mkdtemp(prefix="doc2md-uni-"))
make_fake_install(sandbox)
n_before = sum(1 for p in sandbox.rglob("*") if p.is_file())
cwd_before = os.getcwd()
res = UA.do_uninstall(sandbox, remove_shortcut=False, remove_registry_entry=False)
os.chdir(cwd_before)
check("沙箱里确实有东西（用例有效）", n_before > 40, f"{n_before} 个文件")
check("整个安装目录被删除", not sandbox.exists(), f"仍有残留：{res['leftover']}")
check("没有删不掉的文件", not res["failed"], str(res["failed"]))
check("没有残留报告", not res["leftover"], str(res["leftover"]))
check("摘要里的目录是绝对路径", os.path.isabs(res["dir"]), res["dir"])

print("\n[6] 沙箱卸载：自己就在目录里（交接失败）→ 删掉其余、如实报残留")
sandbox2 = Path(tempfile.mkdtemp(prefix="doc2md-uni2-"))
make_fake_install(sandbox2)
me_fake = sandbox2 / "uninstall.exe"
_orig_self = UA.self_exe
UA.self_exe = lambda: me_fake
cwd_before = os.getcwd()
res2 = UA.do_uninstall(sandbox2, remove_shortcut=False, remove_registry_entry=False)
os.chdir(cwd_before)
UA.self_exe = _orig_self
check("自己（uninstall.exe）被保留下来", me_fake.is_file())
rest = sorted(p.name for p in sandbox2.rglob("*"))
check("其余内容全部删除", rest == ["uninstall.exe"], str(rest))
check("残留被如实报出（界面据此提示用户）",
      res2["leftover"] == ["uninstall.exe"], str(res2["leftover"]))
shutil.rmtree(sandbox2, ignore_errors=True)

print("\n[7] purge_dir 的 keep 语义")
sandbox3 = Path(tempfile.mkdtemp(prefix="doc2md-uni3-"))
make_fake_install(sandbox3)
kept = sandbox3 / "keep-me.txt"
kept.write_text("x")
removed, failed = UA.purge_dir(sandbox3, keep={kept})
check("keep 的文件没被删", kept.is_file())
check("其他文件都删了", sandbox3.exists() and
      sorted(p.name for p in sandbox3.iterdir()) == ["keep-me.txt"],
      str(sorted(p.name for p in sandbox3.iterdir())))
check("顶层的每一项都被删掉（13 项：2 个 exe + _internal + 配置/日志等）",
      len(removed) >= 10, str(len(removed)))
check("没有失败项", not failed, str(failed))
shutil.rmtree(sandbox3, ignore_errors=True)

print("\n[8] 只读文件也要能删掉（装完从光盘/只读介质拷出来的目录）")
sandbox4 = Path(tempfile.mkdtemp(prefix="doc2md-uni4-"))
make_fake_install(sandbox4)
ro = sandbox4 / "readonly.txt"
ro.write_bytes(b"x")
os.chmod(ro, 0o444)
cwd_before = os.getcwd()
res4 = UA.do_uninstall(sandbox4, remove_shortcut=False, remove_registry_entry=False)
os.chdir(cwd_before)
check("含只读文件的目录也被删干净",
      not sandbox4.exists(), f"failed={res4['failed']}")

print("\n[9] 目标目录不存在时不炸（重复卸载是常态）")
cwd_before = os.getcwd()
res5 = UA.do_uninstall(Path(tempfile.gettempdir()) / "doc2md-not-there-xyz",
                       remove_shortcut=False, remove_registry_entry=False)
os.chdir(cwd_before)
check("目标是空/不存在时正常返回", isinstance(res5, dict) and not res5["failed"],
      str(res5))

print("\n[10] 打包配置：卸载程序必须自带提权、且不能排掉 tkinter")
spec = (ROOT / "installer" / "uninstaller.spec").read_text(encoding="utf-8")
check("uninstaller.spec 存在且用 uac_admin=True",
      "uac_admin=True" in spec, "删 Program Files 需要管理员")
check("uninstaller.spec 没排掉 tkinter",
      '"tkinter"' not in spec.split("excludes")[1].split("]")[0],
      "排掉 tkinter 卸载界面会启动即崩")
check("uninstaller.spec 是 onefile（不能依赖 _internal）",
      "COLLECT(" not in spec, "卸载时要删 _internal，自己不能依赖它")

src = (ROOT / "installer" / "uninstall_app.py").read_text(encoding="utf-8")
check("卸载程序会把自己复制到 %TEMP%（否则删不掉自己）",
      "shutil.copy2(me, tmp)" in src)
check("卸载程序把副本登记到重启时清理",
      "MOVEFILE_DELAY_UNTIL_REBOOT" in src or "0x4" in src)
check("卸载程序里没有「脱离的 cmd 延时重试删除」这条路（实测不可靠）",
      "COMSPEC" not in src and "DETACHED_PROCESS" not in src and "creationflags" not in src,
      "实测 rd 在句柄释放后仍长期报占用，只能靠 Python 自己删 + 副本方案")

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
