"""统一入口：打包与直接运行都用它。

三种模式：
  · 带命令行参数  → 等价于 `python -m doc2md <参数>`，便于脚本 / 计划任务调用；
  · 无参数        → 控制台中文菜单（原来的双击行为），
                    但**窗口版 exe（doc2md-gui.exe）无参数时直接开图形界面**；
  · `--gui`       → 强制图形界面；`--menu` 强制控制台菜单。

之所以单独做一个入口，是因为原来双击走的是 `launcher.py`，而 launcher 会
用 `sys.executable -m doc2md` **起子进程**。打包成 exe 后 sys.executable 就是
exe 自己，那样会无限自我递归 —— 所以打包版的子命令由 launcher 在同进程内直调。

窗口版与控制台版**共用同一份 Analysis/PYZ**（见 doc2md.spec），所以这里的
分发逻辑对两个 exe 是同一份代码，靠下面 _prefer_gui() 区分该进哪一边。
"""
from __future__ import annotations

import sys
from pathlib import Path

from doc2md.stdio import make_stdio_safe


def _is_windowed_build() -> bool:
    """打包成 console=False 的窗口版时，PyInstaller 不会挂真实的 stdout。

    它可能是 None，也可能是个「写入即丢弃」的哑对象 —— 两种情况都要认出来。
    注意：只在 frozen 下才用这个判断，源码运行 `python app.py` 不该因此跳到 GUI。
    """
    if not getattr(sys, "frozen", False):
        return False
    out = sys.stdout
    if out is None:
        return True
    try:
        out.fileno()
        return False
    except Exception:
        return True


def _prefer_gui() -> bool:
    """双击时该不该进图形界面。

    两个信号，命中任一即为窗口版：
      1. 可执行文件名里带 gui（doc2md-gui.exe）—— 用户手动改名也照样好使；
      2. 没有真实控制台（console=False 的构建）。
    两个都不是 → 控制台菜单，保持老行为不变。
    """
    try:
        if "gui" in Path(sys.executable).stem.lower():
            return True
    except Exception:
        pass
    return _is_windowed_build()


def main() -> int:
    # 第一件事：把控制台的编码策略放宽。必须在任何 print 之前 —— 尤其是
    # 打包版报错的那一刻，否则一个编不出的装饰字符会把原始异常顶掉。
    make_stdio_safe()

    argv = list(sys.argv[1:])
    force_gui = "--gui" in argv
    force_menu = "--menu" in argv
    argv = [a for a in argv if a not in ("--gui", "--menu")]

    # 有子命令 → 命令行优先（脚本 / 计划任务靠它）
    if argv:
        from doc2md.cli import main as cli_main

        # 显式传 argv：上面已经摘掉了 --gui / --menu，不能让 cli 再去读 sys.argv
        return cli_main(argv)

    if force_gui or (not force_menu and _prefer_gui()):
        from gui import main as gui_main

        return gui_main()

    from launcher import main as menu_main

    return menu_main()


if __name__ == "__main__":
    sys.exit(main())
