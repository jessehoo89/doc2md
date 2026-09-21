"""命令行入口。

  运行方式（两条都要满足，否则会「没有任何输出」或 import 失败）：
    1. 用装好依赖的解释器，**不要**用系统 python（PATH 首家若是 WindowsApps
       别名，命中的是 Store 占位程序：静默退出 9009、零输出）
    2. 工作目录 = 仓库根（本 package 的上一级），或自行设 PYTHONPATH
  最省事：双击仓库根下的 云端OCR自检.bat（ping+env）或 文档转MD.bat（菜单）。

  python -m doc2md scan            扫描并试运行（不写文件）
  python -m doc2md run             批量转换全部
  python -m doc2md watch           实时监控模式
  python -m doc2md test <文件>     转换单个文件
  python -m doc2md status          查看统计（含各后端今日用量）
  python -m doc2md retry           重试失败的文件
  python -m doc2md ping            自检各云端 OCR 后端的就绪与连通性
  python -m doc2md env             查看凭据文件（.env）与各后端 Token 的生效情况
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .config import (
    ENV_FILE,
    Config,
    describe_backends,
    describe_credentials,
    describe_env_file,
    describe_output,
    load_config,
    mask_token,
)
from .engine import Engine
from .state import StateStore


def _install_thread_guard() -> None:
    """屏蔽第三方库在子线程里的已知无害噪音。

    pymupdf4llm 的 ONNX 版面模型会拉起子进程，其 stderr 读取线程把输出
    当 UTF-8 解码，在中文 Windows（GBK）下必然抛 UnicodeDecodeError 并
    打到控制台，非常吓人但其实不影响结果。这里把它静音，其余异常照常抛出。
    """
    import threading

    original = threading.excepthook

    def hook(args):
        exc = args.exc_value
        if isinstance(exc, UnicodeDecodeError) and "_readerthread" in str(args.thread.name):
            return
        if isinstance(exc, UnicodeDecodeError) and args.thread.name.startswith("Thread-"):
            return
        original(args)

    threading.excepthook = hook


def _banner(cfg: Config, mode: str) -> None:
    print("=" * 74)
    print(f"  文档批量转 Markdown  ·  {mode}")
    print("=" * 74)
    print(f"  处理目录 : {', '.join(cfg.roots)}")
    print(f"  转换格式 : {', '.join(cfg.watch_extensions)}")
    print(f"  保留原文件: {'是' if cfg.keep_original else '否'}")
    print(f"  md 输出到 : {describe_output(cfg)}")
    chain = describe_backends(cfg)
    n_backends = len([b for b in cfg.ocr_backends if b.enabled])
    print(f"  云端 OCR : {'启用' if cfg.ocr.enabled else '禁用'}"
          f"（{n_backends} 个后端，故障熔断自动切换；"
          f"敏感目录{'已拦截' if cfg.shield_sensitive_for_ocr else '未拦截'}）")
    print(f"  后端链路 : {chain}")
    if cfg.env_file is not None:
        print(f"  凭据文件 : {cfg.env_file}（生效 {len(cfg.env_keys)} 项）")
    else:
        print(f"  凭据文件 : 未找到 {ENV_FILE}"
              f"（Token 只能写在 config.json 或系统环境变量里）")
    if getattr(cfg, "local_ocr", None) is not None:
        lo = cfg.local_ocr
        print(f"  本地 OCR : {'启用' if lo.enabled else '禁用'}"
              f"（RapidOCR PP-OCRv6，{lo.device}，"
              f"复杂表{'转云端' if lo.ocr_complex_fallback else '本地启发式'}）")
    print("=" * 74)
    print()


def cmd_scan(args, cfg: Config) -> int:
    _banner(cfg, "试运行 / 扫描")
    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=False)
    eng.run(dry_run=True, limit=args.limit)
    store.close()
    print("\n[提示] 试运行未写入任何文件。确认无误后运行： python -m doc2md run")
    return 0


def cmd_run(args, cfg: Config) -> int:
    if args.no_ocr:
        cfg.ocr.enabled = False
    _banner(cfg, "批量转换")
    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=not args.quiet)
    t0 = time.time()
    rep = eng.run(dry_run=False, limit=args.limit)
    store.close()

    print("\n" + "=" * 74)
    print("  转换完成")
    print("=" * 74)
    print(f"  成功 {rep.ok}   跳过 {rep.skipped}   失败 {rep.failed}   拦截 {rep.blocked}")
    if rep.deferred:
        print(f"  暂缓待重试 {rep.deferred}")
    if rep.ocr_pages:
        print(f"  云端 OCR：{rep.ocr_files} 个文件 / {rep.ocr_pages} 页")
    if rep.ocr_by_backend:
        dist = "，".join(f"{k}={v}" for k, v in rep.ocr_by_backend.most_common())
        print(f"  后端用量：{dist}")
    if rep.ocr_switched:
        print(f"  熔断切换：{rep.ocr_switched} 个文件由备用后端接手")
    if rep.ocr_missing_images:
        print(f"  [注意] {rep.ocr_missing_images} 个文件的插图未被后端返回（md 中留有空占位）")
    print(f"  耗时 {time.time() - t0:.1f} 秒")
    if rep.ocr_paused:
        print("\n  [注意] 云端 OCR 链路全部不可用，本轮已熔断剩余 OCR 任务。")
        print("         本地转换（Office / 文本 PDF）不受影响，已全部完成。")
        print("         稍后再次运行即可续跑这些文件，会自动跳过已完成的。")
    if rep.by_engine:
        print("  按引擎分布：" + "，".join(f"{k}={v}" for k, v in rep.by_engine.most_common()))
    if rep.errors:
        print(f"\n  失败明细（前 15 条，共 {len(rep.errors)}）：")
        for p, e in rep.errors[:15]:
            print(f"    - {Path(p).name}\n        {e}")
        print("\n  可用 python -m doc2md retry 重试这些文件。")
    print("=" * 74)
    return 0 if rep.failed == 0 else 1


def cmd_watch(args, cfg: Config) -> int:
    from .watcher import WatchService

    if args.no_ocr:
        cfg.ocr.enabled = False
    _banner(cfg, "实时监控")
    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=False)
    svc = WatchService(cfg, store, eng, logger=lambda m: print(m, flush=True),
                       config_path=args.config)
    try:
        svc.start(catch_up=not args.no_catch_up)
    except KeyboardInterrupt:
        print("\n收到停止信号…")
        svc.stop()
    finally:
        store.close()
    return 0


def cmd_test(args, cfg: Config) -> int:
    _banner(cfg, "单文件测试")
    target = Path(args.path).resolve()
    if not target.exists():
        print(f"[错误] 文件不存在：{target}")
        return 2
    if not target.is_absolute():
        print("[错误] 请使用绝对路径")
        return 2
    store = StateStore(cfg.state_db)
    eng = Engine(cfg, store, verbose=True)
    t = eng.plan(target)
    if t is None:
        print(f"[跳过] 该格式不在转换范围内，或内容无法识别：{target.name}")
        store.close()
        return 0
    print(f"  真实格式 : {t.kind.value}")
    print(f"  处理路线 : {t.route}")
    if t.pages:
        print(f"  页数     : {t.pages}")
    if t.note:
        print(f"  备注     : {t.note}")
    out_path = eng.md_path_for(target)
    print(f"  将输出到 : {out_path}")
    print()
    status, info = eng.process(t)
    store.close()
    md = out_path
    print(f"\n  结果：{status}  ({info})")
    if md.exists():
        text = md.read_text(encoding="utf-8-sig")
        print(f"  输出：{md}")
        print(f"  字数：{len(text)}")
        print("  预览：")
        for line in text.splitlines()[:12]:
            print("    " + line[:88])
    return 0 if status in ("ok", "skip") else 1


def cmd_status(args, cfg: Config) -> int:
    store = StateStore(cfg.state_db)
    s = store.stats()
    print("=" * 74)
    print("  转换状态统计")
    print("=" * 74)
    print(f"  数据库   : {cfg.state_db}")
    print(f"  已记录   : {s['total']} 个文件")
    for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1]):
        print(f"    {k:<10} {v}")
    if s["by_engine"]:
        print("  按引擎：")
        for k, v in list(s["by_engine"].items())[:12]:
            print(f"    {k:<12} {v}")
    print(f"  累计页数 : {s['pages_total']}（今日 {s['pages_today']}）")
    bu = s.get("backend_pages_today") or {}
    if bu:
        print("  今日后端用量：")
        for name, pages in bu.items():
            lim = 0
            for b in cfg.ocr_backends:
                if b.name == name:
                    lim = b.daily_page_limit
                    break
            print(f"    {name:<16} {pages} 页" + (f" / 上限 {lim}" if lim else "（不限）"))
    else:
        print(f"    今日云端 OCR 配额上限：{cfg.ocr.daily_page_limit} 页（分后端计）")
    if s["recent_failures"]:
        print(f"\n  最近失败 {len(s['recent_failures'])} 条：")
        for f in s["recent_failures"][:10]:
            print(f"    - {Path(f['path']).name}\n        {f['error'][:120]}")
    print("=" * 74)
    store.close()
    return 0


def cmd_retry(args, cfg: Config) -> int:
    store = StateStore(cfg.state_db)
    paths = store.retry_paths()
    if args.clear:
        n = store.reset_failed()
        print(f"已清除 {n} 条失败记录。")
        store.close()
        return 0
    if not paths:
        print("没有需要重试的文件。")
        store.close()
        return 0
    print(f"待重试 {len(paths)} 个文件（含上轮暂缓的）…\n")
    eng = Engine(cfg, store, verbose=not args.quiet)
    ok = fail = 0
    for p in paths:
        fp = Path(p)
        if not fp.exists():
            store.mark_skipped(fp, 0, 0, "retry", "源文件已不存在")
            continue
        try:
            st = fp.stat()
            t = eng.plan(fp)
            if t is None:
                continue
            status, info = eng.process(t)
            print(f"  [{status}] {fp.name}")
            ok += status == "ok"
            fail += status == "fail"
        except Exception as e:
            fail += 1
            print(f"  [fail] {fp.name}: {e}")
    eng.close()
    store.close()
    print(f"\n重试完成：成功 {ok}，仍失败 {fail}")
    return 0


def cmd_ping(args, cfg: Config) -> int:
    """检测每个云端 OCR 后端的就绪状态与连通性。"""
    from .ocr_router import build_router

    print("=" * 74)
    print("  云端 OCR 后端自检")
    print("=" * 74)
    print(f"  后端链路 : {describe_backends(cfg)}")
    print(f"  凭据文件 : {describe_env_file(cfg)}")
    print("  后端凭据 :")
    for line in describe_credentials(cfg):
        print(f"    · {line}")
    print()
    router = build_router(cfg, store=None, verbose=True, logger=print)
    if router is None:
        print("[失败] 没有任何可用的云端 OCR 后端")
        print("       检查 config.json 的 ocr.backends，以及 .env 里的 Token 是否已填写。")
        print(f"       凭据文件位置：{ENV_FILE}")
        return 1
    results = router.ping_all()
    ok_n = 0
    for name, ok, msg in results:
        ok_n += bool(ok)
        print(f"  [{'成功' if ok else '失败'}] {name:<16} {msg}")
    print()
    print(f"  可用后端 {ok_n}/{len(results)}"
          + ("，熔断切换链路已就绪。" if ok_n > 1 else
             "，只有一个后端可用，无冗余切换能力。" if ok_n == 1 else "。"))
    print("=" * 74)
    return 0 if ok_n > 0 else 1


# 工具认得的凭据键（按展示顺序），值是一句话用途说明
_CRED_KEYS: tuple[tuple[str, str], ...] = (
    ("DOC2MD_MINERU_TOKEN", "MinerU Token（precision / agent 通用）"),
    ("MINERU_TOKEN", "MinerU Token 的旧别名"),
    ("DOC2MD_MINERU_BASE_URL", "MinerU 服务地址（自建 / 代理才需要）"),
    ("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN", "PaddleOCR 访问令牌（官方 MCP 用的名字）"),
    ("DOC2MD_PADDLE_TOKEN", "PaddleOCR 访问令牌（别名，优先级更高）"),
    ("DOC2MD_SILICONFLOW_TOKEN", "硅基流动 Token（sf-deepseek-ocr 后端）"),
    ("SILICONFLOW_API_KEY", "硅基流动 Token 的别名"),
    ("DOC2MD_VLM_TOKEN", "通用 VLM 兜底 Token（不按厂商区分，慎用）"),
    ("DOC2MD_VLM_BASE_URL", "VLM 服务地址（换平台才需要）"),
)

_ENV_TEMPLATE = """
# 云端 OCR 凭据文件（改完需重启程序）
DOC2MD_MINERU_TOKEN=

PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN=

DOC2MD_SILICONFLOW_TOKEN=
"""


def cmd_env(args, cfg: Config) -> int:
    """查看凭据文件与各后端 Token 的生效情况（不打印 Token 明文）。"""
    print("=" * 74)
    print("  云端 OCR 凭据自检")
    print("=" * 74)
    print(f"  凭据文件 : {describe_env_file(cfg)}")
    print("  优先级   : 系统环境变量 > .env 文件 > config.json 的 token 字段")
    print()
    if cfg.env_file is None:
        print(f"  [提示] 没有找到凭据文件：{ENV_FILE}")
        print("         在该路径新建一个文本文件（注意文件名就叫 .env），内容照抄：")
        print(_ENV_TEMPLATE)
    else:
        in_file = set(cfg.env_keys)
        print("  文件中的键：")
        for key, why in _CRED_KEYS:
            val = (os.environ.get(key) or "").strip()
            if val:
                src = ".env" if key in in_file else "系统环境变量"
                print(f"    [已填] {key:<38} {mask_token(val)}　← {src}")
            else:
                print(f"    [空  ] {key:<38} {why}")
        others = sorted(in_file - {k for k, _ in _CRED_KEYS})
        if others:
            print("  其它键（本工具不识别，仅列出）：")
            for key in others:
                print(f"    · {key} = {mask_token(os.environ.get(key, ''))}")
    print()
    print("  各后端实际凭据：")
    for line in describe_credentials(cfg):
        print(f"    · {line}")
    print()
    print("  说明：MinerU 轻量接口（agent）免鉴权，不填 Token 也能当兜底；")
    print("        但**只有 precision 模式会返回插图**，要插图就必须填 Token。")
    print("        sf-deepseek-ocr（硅基流动）只出文字不返回插图，排在需要插图的后端之后。")
    print("        改完凭据文件后需要重启程序；填好后用 python -m doc2md ping 验证连通性。")
    print("=" * 74)
    return 0


def _add_common(p: argparse.ArgumentParser, suppress: bool) -> None:
    """公共参数。suppress=True 用于子命令，避免其默认值把主解析器的结果覆盖掉。"""
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--config", default=d, metavar="路径", help="配置文件路径")
    p.add_argument("--root", action="append", default=d, metavar="目录",
                   help="覆盖处理目录，可重复指定")
    p.add_argument("--limit", type=int, default=(argparse.SUPPRESS if suppress else 0),
                   metavar="N", help="最多处理多少个文件（调试用）")

    def _flag(name: str, help_text: str) -> None:
        p.add_argument(name, action="store_true",
                       default=argparse.SUPPRESS if suppress else False, help=help_text)

    _flag("--quiet", "只输出汇总，不逐条打印")
    _flag("--no-ocr", "本次禁用云端 OCR")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doc2md",
        description="文档批量转 Markdown（docx/doc/xls/xlsx/pdf → md，支持 OCR 与实时监控）",
    )
    _add_common(p, suppress=False)
    p.add_argument("--version", action="version", version="doc2md 1.0.0")

    sub = p.add_subparsers(dest="cmd", metavar="命令")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        # 子命令上也挂一份公共参数，这样写在命令后面也能识别
        sp = sub.add_parser(name, help=help_text, parents=[])
        _add_common(sp, suppress=True)
        return sp

    add("scan", "扫描并试运行，不写文件")
    add("run", "批量转换全部文件")
    w = add("watch", "实时监控目录并自动转换")
    w.add_argument("--no-catch-up", action="store_true", help="启动时不先做全量扫描")
    t = add("test", "转换单个文件")
    t.add_argument("path", help="待转换文件的绝对路径")
    add("status", "查看转换统计")
    r = add("retry", "重试失败的文件")
    r.add_argument("--clear", action="store_true", help="仅清除失败记录，不重试")
    add("ping", "检测 OCR 服务连通性")
    add("env", "查看凭据文件（.env）与各后端 Token 的生效情况")
    return p


def main(argv: list[str] | None = None) -> int:
    _install_thread_guard()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    cfg = load_config(args.config)
    if args.root:
        cfg.roots = args.root

    handlers = {
        "scan": cmd_scan,
        "run": cmd_run,
        "watch": cmd_watch,
        "test": cmd_test,
        "status": cmd_status,
        "retry": cmd_retry,
        "ping": cmd_ping,
        "env": cmd_env,
    }
    try:
        return handlers[args.cmd](args, cfg)
    except KeyboardInterrupt:
        print("\n已中断。进度已保存，再次运行会从断点继续。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
