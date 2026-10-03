"""命令行入口。

  运行方式（两条都要满足，否则会「没有任何输出」或 import 失败）：
    1. 用装好依赖的解释器，**不要**用系统 python（PATH 首家若是 WindowsApps
       别名，命中的是 Store 占位程序：静默退出 9009、零输出）
    2. 工作目录 = 仓库根（本 package 的上一级），或自行设 PYTHONPATH
  最省事：双击仓库根下的 云端OCR自检.bat（ping+env）或 文档转MD.bat（菜单）。

  python -m doc2md scan            扫描并试运行（不写文件）
  python -m doc2md run             批量转换全部
  python -m doc2md convert <文件…>  只转换指定的文件 / 清单（见下）
  python -m doc2md watch           实时监控模式
  python -m doc2md test <文件>     转换单个文件（看分流详情用）
  python -m doc2md status          查看统计（含各后端今日用量）
  python -m doc2md retry           重试失败的文件
  python -m doc2md ping            自检各云端 OCR 后端的就绪与连通性
  python -m doc2md env             查看凭据文件（.env）与各后端 Token 的生效情况

`convert` —— 只转"这批文件"，不扫整目录。三种给法（可混用）：
  python -m doc2md convert a.docx b.pdf "D:\\语料\\某目录"
  python -m doc2md convert --list files.txt          # 一行一个路径，# 为注释
  dir /b /s *.pdf | python -m doc2md convert --list -
加 --dry-run 先看分流预览（不写文件），加 --force 强制重转（否则已转过的会跳过）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import filelist
from .config import (
    ENV_FILE,
    Config,
    cred_key_list,
    describe_backends,
    describe_credentials,
    describe_env_file,
    describe_output,
    describe_tokens,
    env_template_text,
    load_config,
    mask_token,
)
from .engine import Engine
from .state import StateStore
from .stdio import make_stdio_safe


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


def _banner(cfg: Config, mode: str, roots_text: str | None = None) -> None:
    print("=" * 74)
    print(f"  文档批量转 Markdown  ·  {mode}")
    print("=" * 74)
    print(f"  处理目录 : {roots_text if roots_text is not None else ', '.join(cfg.roots)}")
    print(f"  转换格式 : {', '.join(cfg.watch_extensions)}")
    print(f"  保留原文件: {'是' if cfg.keep_original else '否'}")
    print(f"  md 输出到 : {describe_output(cfg)}")
    if cfg.pdf_engine == "rule":
        print("  文本 PDF : rule 档（纯规则、零模型，不加载 ONNX 版面模型）")
    else:
        print("  文本 PDF : layout 档（ONNX 版面模型，约 49MB）")
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


def _print_summary(rep, elapsed: float) -> None:
    """打印转换汇总。`run` 与 `convert` 共用这一份 —— 统计项只写一遍，
    免得以后加一项只顾得上改其中一处。"""
    print("\n" + "=" * 74)
    print("  转换完成")
    print("=" * 74)
    print(f"  成功 {rep.ok}   跳过 {rep.skipped}   失败 {rep.failed}   拦截 {rep.blocked}")
    notable = rep.notable_skips()
    if notable:
        # 空文档 / 已加密 这类不是失败，但也别让它们悄悄消失
        dist = "，".join(f"{k} {v}" for k, v in notable[:5])
        print(f"       其中：{dist}")
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
    print(f"  耗时 {elapsed:.1f} 秒")
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


def cmd_run(args, cfg: Config) -> int:
    if args.no_ocr:
        cfg.ocr.enabled = False
    _banner(cfg, "批量转换")
    store = StateStore(cfg.state_db)
    redo = (getattr(args, "redo_engine", "") or "").strip()
    if redo:
        # 换了引擎（或改了转换逻辑）后，状态库里"已成功"的记录会让文件被跳过。
        # 只删记录、不删 md —— 重转会覆盖它们。
        n = store.reset_by_engine(redo)
        print(f"  [重转] 已清除 engine={redo} 的 {n} 条记录（md 不动，重转时覆盖）")
        if n == 0:
            print(f"         当前库里有这些引擎：{store.engines()}")
        print()
    eng = Engine(cfg, store, verbose=not args.quiet)
    t0 = time.time()
    rep = eng.run(dry_run=False, limit=args.limit)
    store.close()
    _print_summary(rep, time.time() - t0)
    return 0 if rep.failed == 0 else 1


def _merge_lists(dst, src) -> None:
    """把两份清单合并进 dst（路径按小写去重，与状态库口径一致）。"""
    seen = {str(p).lower() for p in dst.paths}
    for p in src.paths:
        key = str(p).lower()
        if key in seen:
            dst.duplicates += 1
        else:
            seen.add(key)
            dst.paths.append(p)
    dst.missing += src.missing
    dst.unsupported += src.unsupported
    dst.from_dirs += src.from_dirs
    dst.duplicates += src.duplicates


def _outside_roots(paths, roots) -> list[Path]:
    """挑出不在任何处理目录之下的文件。

    与输出结构有关：custom + mirror 模式下，只有位于某个 root 之下的文件才能算出
    相对路径，其余会退化成"按文件名平铺到输出根"。清单模式很容易撞上这件事
    （文件本来就是从别处拷来的），所以提前提示，而不是等用户发现 md 全堆在一起。
    """
    out: list[Path] = []
    resolved = []
    for r in roots or []:
        try:
            resolved.append(Path(r).resolve())
        except OSError:
            continue
    for p in paths:
        try:
            ap = p.resolve()
        except OSError:
            ap = p
        hit = False
        for r in resolved:
            try:
                ap.relative_to(r)
                hit = True
                break
            except ValueError:
                continue
        if not hit:
            out.append(p)
    return out


def cmd_convert(args, cfg: Config) -> int:
    """按清单批量转换：只处理点名的文件，不去扫 `cfg.roots`。

    存在的理由：日常大量场景是"就转这一批"，而整目录跑要么把所有无关文件也带上，
    要么得反复改 config 的 roots。这里把清单当作"临时的一组根目录"，转换本身
    仍走 Engine 的既有流程 —— 幂等跳过、敏感拦截、故障熔断、断点续传一律照旧。
    """
    if args.no_ocr:
        cfg.ocr.enabled = False

    parts = []
    if args.list:
        try:
            lines, enc = filelist.read_manifest(args.list)
        except FileNotFoundError as e:
            print(f"[错误] {e}")
            return 2
        except OSError as e:
            print(f"[错误] 读取清单失败：{e}")
            return 2
        if args.list == "-":
            base, src_name = Path.cwd(), "标准输入"
        else:
            mp = Path(args.list).expanduser().resolve()
            base, src_name = mp.parent, str(mp)
        one = filelist.collect(lines, cfg=cfg, base_dir=base,
                               source=f"{src_name}（编码 {enc}）")
        one.encoding = enc
        parts.append(one)

    if args.paths:
        parts.append(filelist.collect(args.paths, cfg=cfg, source="命令行参数"))

    if not parts:
        print("[错误] 没有指定要转换的文件。三种用法：")
        print("        doc2md convert a.pdf b.docx            直接列文件")
        print("        doc2md convert --list files.txt        从清单文件读")
        print("        dir /b /s *.pdf | doc2md convert --list -   从管道读")
        return 2

    res = parts[0]
    for more in parts[1:]:
        _merge_lists(res, more)

    _banner(cfg, "清单批量转换", roots_text="（忽略 —— 只转清单里点名的文件）")
    print(f"  清单来源 : {res.source}")
    print(f"  解析结果 : {res.summary()}")
    if res.missing:
        print(f"\n  [找不到] {len(res.missing)} 条（清单里写了，磁盘上没有）：")
        for m in res.missing[:10]:
            print(f"    - {m}")
        if len(res.missing) > 10:
            print(f"    …另有 {len(res.missing) - 10} 条")
    if res.unsupported:
        print(f"\n  [格式不支持] {len(res.unsupported)} 个"
              f"（不在 config 的 watch_extensions 里，会被跳过）：")
        for u in res.unsupported[:10]:
            print(f"    - {u}")
        if len(res.unsupported) > 10:
            print(f"    …另有 {len(res.unsupported) - 10} 个")
    if not res.ok:
        print("\n[错误] 清单里没有任何可转换的文件，已中止。")
        print("       检查上面的「找不到 / 格式不支持」，或看看是不是漏了 --list 的参数。")
        return 2

    if cfg.output.is_custom and cfg.output.layout == "mirror":
        outside = _outside_roots(res.paths, cfg.roots)
        if outside:
            print(f"\n  [提示] {len(outside)} 个文件不在处理目录之下，"
                  f"mirror 模式下会平铺到输出根目录。")
            print("         想保留原本的目录结构，用 --root 指定它们的上级目录。")

    print()
    store = StateStore(cfg.state_db)
    try:
        if args.force and not args.dry_run:
            n = store.forget(res.paths)
            print(f"  [重转] --force：已清除清单内文件的状态记录"
                  f"（实际命中 {n} 条；已产出的 md 不动，重转时覆盖）\n")
        elif args.force:
            # dry-run 是无副作用的预览，别在这里把状态库清了 —— 否则"先看看再决定"
            # 的人会发现记录已经没了，真跑时反而变成全量重转。
            print("  [提示] --dry-run 与 --force 同用：只预览，不清状态记录、不写文件。\n")
        eng = Engine(cfg, store, verbose=not args.quiet)
        t0 = time.time()
        try:
            rep = eng.run(dry_run=args.dry_run, limit=args.limit, paths=res.paths,
                          force=args.force)
        finally:
            eng.close()
    finally:
        store.close()

    if args.dry_run:
        print("\n[提示] --dry-run 只做分流预览，未写入任何文件。去掉该参数即可真正转换。")
        return 0
    _print_summary(rep, time.time() - t0)
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
    if t.route in ("encrypted", "empty"):
        # 这两类不会产出 md，别打印一个不存在的输出路径误导人
        out_path = None
    else:
        out_path = eng.md_path_for(target)
        print(f"  将输出到 : {out_path}")
    print()
    status, info = eng.process(t)
    store.close()
    md = out_path
    print(f"\n  结果：{status}  ({info})")
    if md is not None and md.exists():
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
    if s.get("skipped_reasons"):
        print("  跳过原因（前 10）：")
        for reason, n in s["skipped_reasons"]:
            print(f"    {n:>6}  {reason[:70]}")
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


# 工具认得的凭据键清单（含别名与用途说明）来自 config.CRED_FIELDS 这一份唯一定义，
# 免得界面能填、命令行却报"未配置"。
_CRED_KEYS: tuple[tuple[str, str], ...] = cred_key_list()

_ENV_TEMPLATE = env_template_text()


def cmd_env(args, cfg: Config) -> int:
    """查看凭据文件与各后端 Token 的生效情况（不打印 Token 明文）。"""
    print("=" * 74)
    print("  云端 OCR 凭据自检")
    print("=" * 74)
    print(f"  凭据文件 : {describe_env_file(cfg)}")
    print("  优先级   : 系统环境变量 > .env 文件 > config.json 的 token 字段")
    print(f"  填写情况 : {describe_tokens()}")
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
    print("        填好后用 `python -m doc2md ping` 或图形界面的「检测云端 OCR」验证连通性。")
    print("        图形界面里可以直接填：工具栏「设置…」里的「云端 OCR Token」页，保存即生效。")
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
    r = add("run", "批量转换全部文件（扫 cfg.roots；只转指定文件请用 convert）")
    r.add_argument("--redo-engine", metavar="引擎", default=argparse.SUPPRESS,
                   help="先清掉状态库里该引擎的记录再跑（换引擎后重转用），"
                        "如 pdf-text / docx / xlsx")
    c = add("convert", "只转换指定的文件（命令行给路径，或用 --list/-l 给清单文件）")
    c.add_argument("paths", nargs="*", metavar="路径",
                   help="要转换的文件或目录，可给多个；目录会按配置的扩展名递归展开")
    c.add_argument("--list", "-l", dest="list", metavar="清单",
                   help="清单文件路径，每行一个（# 开头为注释、空行忽略、"
                        "支持 UTF-8/GBK/UTF-16 编码）；给 - 表示从标准输入读")
    c.add_argument("--dry-run", action="store_true",
                   help="只解析清单并预览分流与 OCR 量，不写任何文件")
    c.add_argument("--force", action="store_true",
                   help="忽略状态库强制重转清单内的文件（已产出的 md 会被覆盖）")
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
    make_stdio_safe()
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
        "convert": cmd_convert,
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
