"""跑 `doc2md run` 并把输出以 UTF-8 写进日志。

为什么要这个脚本：直接从 PowerShell 管道接管 python 的 stdout，PowerShell 会用
控制台的本地代码页（中文 Windows 上是 GBK）去解码，而 python 输出的是 UTF-8 字节，
结果日志里的中文全成乱码（双重编码）。让 python 自己写文件就绕开了这一层。

用法：
    python run_and_log.py                     # 全量转换，日志写默认位置
    python run_and_log.py --tag reconvert4    # 换个日志名后缀
    python run_and_log.py -- retry            # 透传给 doc2md 的子命令（默认 run）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"


def main() -> int:
    argv = sys.argv[1:]
    tag = "reconvert3"
    if "--tag" in argv:
        i = argv.index("--tag")
        tag = argv[i + 1]
        del argv[i:i + 2]
    if "--" in argv:
        i = argv.index("--")
        cmd = argv[i + 1:] or ["run"]
    else:
        cmd = argv or ["run"]

    log = ROOT / f"run_{tag}.log"
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    f = open(log, "w", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f

    import time

    print(f"### doc2md {' '.join(cmd)} 启动于 {time.strftime('%Y-%m-%d %H:%M:%S')} ###")
    print(f"### 日志: {log}")
    print()

    from doc2md.cli import main as cli_main  # noqa: E402

    code = 0
    try:
        code = cli_main(cmd + ["--config", str(CONFIG)])
    except BaseException as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        code = 1
        print(f"\n### 异常终止: {type(e).__name__}: {e}")
    finally:
        print(f"\n### 结束于 {time.strftime('%Y-%m-%d %H:%M:%S')} exit={code} ###")
        f.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
