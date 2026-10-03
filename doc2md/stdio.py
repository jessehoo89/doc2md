"""让控制台 / 管道的输出永不因编码失败而中断。

单独做成一个小模块，是因为它必须在**每个入口最早**装上：

    app.py       打包后的唯一入口（两个 exe 都走它）
    launcher.py  文档转MD.bat 直接调它，绕过 app.py
    cli.py       命令行
    gui.py       图形界面

挂进 `doc2md.cli` 会顺手把整个引擎拖进来，图形界面「秒开」就没了 —— 所以
这里刻意只依赖 `sys`。

背景（实测踩过的坑，两个都是它造成的）
------------------------------------
1. Windows 控制台默认是 GBK，而文案里用了 ⭐ ⚠ ↳ ↔ ✓ ✗ ⊘ 这类符号。
   `print("⭐ ...")` 编不进 GBK 直接抛 UnicodeEncodeError。
2. 更糟的是它发生在**打印报错信息的那一刻**：引擎要打
   `      ↳ FileNotFoundError: ...`，`↳` 编不出去 → UnicodeEncodeError 盖住
   了原始异常 → 屏幕上只剩 `Failed to execute script 'app' due to unhandled
   exception!`。查一个「文本层 PDF 全失败」的问题，却被引去翻编码，白绕一圈。

所以：编码策略放宽成 replace，编不出的字符显示成 `?`，绝不抛异常、绝不掩盖
原始错误。再配合把控制台用的装饰符号换成 GBK 里有的字符（见 launcher.MENU
的 ★），console 下看起来也正常。
"""
from __future__ import annotations

import sys

__all__ = ["make_stdio_safe"]


def make_stdio_safe() -> None:
    """把 stdout / stderr 的编码错误策略放宽成 ``replace``。

    - 可重复调用，重复调用无副作用；
    - 窗口版（console=False）下 ``sys.stdout`` 可能是 ``None`` 或哑对象，
      已替换成自定义实现的流也不认 ``reconfigure`` —— 一律容错跳过。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")
        except Exception:          # 非文本流 / 哑对象 / 已被替换掉的实现
            pass
