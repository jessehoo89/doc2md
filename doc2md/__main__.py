"""支持 `python -m doc2md` 调用。"""
import sys
from pathlib import Path

# 仓库根 = 本包的上一级。config.json / .env / state.db / logs 都在那里，
# 所以从任何位置调用本模块，工作目录都应当先切到这里。
_ROOT = Path(__file__).resolve().parent.parent


def _hint(exc: BaseException) -> None:
    """解释器/依赖不对时给出可照抄的修复步骤，而不是甩一段 traceback。

    最常见的两种翻车方式：
      1. 用系统 python 启动（PATH 首项若是 WindowsApps 别名目录，命中的是
         Store 占位程序 —— 静默退出 9009、零输出，连本提示都不会出现）；
      2. 用了没装依赖（requests / pymupdf / mammoth / openpyxl …）的解释器，
         导入阶段即失败。
    """
    print("=" * 74)
    print("  [错误] doc2md 无法启动：Python 解释器不对，或缺少依赖")
    print("=" * 74)
    print(f"  当前解释器：{sys.executable}")
    print(f"  详细原因　：{type(exc).__name__}: {exc}")
    print()
    print("  请改用装好依赖的解释器（推荐项目自带的虚拟环境）：")
    print(f'    "{_ROOT}\\.venv\\Scripts\\python.exe" -m doc2md ...')
    print("  工作目录也要在仓库根，这样才 import 得到 doc2md 包：")
    print(f'    cd /d "{_ROOT}"')
    print(f'    set PYTHONPATH={_ROOT}')
    print()
    print("  虚拟环境还没建好时（建一次即可）：")
    print(f'    python -m venv "{_ROOT}\\.venv"')
    print(f'    "{_ROOT}\\.venv\\Scripts\\python.exe" -m pip install -r requirements.txt')
    print()
    print("  最省事的方式（都不用敲命令）：双击仓库根下的  文档转MD.bat  用菜单操作")
    print("=" * 74)


try:
    from .cli import main
except ImportError as _exc:  # pragma: no cover - 环境问题，非代码问题
    _hint(_exc)
    raise SystemExit(2)

if __name__ == "__main__":
    sys.exit(main())
