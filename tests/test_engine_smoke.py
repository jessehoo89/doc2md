# -*- coding: utf-8 -*-
"""端到端冒烟测试：真实跑一次引擎，验证"熔断切换"在完整链路上成立。

场景（故意把链路首端设成坏后端）：
  后端 1  broken-paddle —— base_url 指向 127.0.0.1:9（无人监听），必然连不通
  后端 2  mineru-agent  —— MinerU 免 Token 轻量接口，真实可跑

预期：
  · 第 1 个文件：broken-paddle 连接失败（network）→ 熔断 → 自动切换 mineru-agent 成功；
                 报告里 ocr_switched +1、attempts 有两个后端
  · 第 2、3 个文件：broken-paddle 已在熔断冷却中 → 不再发起新请求，直接走 mineru-agent
  · 备用后端不会被误判为鉴权失败（Paddle 的 token 不会串到 MinerU 上）
  · 结束时可查询每个后端的熔断状态

全程隔离在临时目录里，不碰真实语料与 state.db。

用法：python test_engine_smoke.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = Path(ROOT) / "config.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 隔离真实凭据：本测试不得读到生产 .env（否则真实 Token 会串进来，断言失去意义）
os.environ["DOC2MD_ENV_FILE"] = "none"

LOG_PATH = Path(__file__).with_name("test_engine_smoke.log")

# 挑 3 个小扫描件（≤10MB，适配 mineru-agent 的 10MB / 20 页上限）。
# 用引擎同一套判定筛出"真的会走 OCR"的 PDF，避免挑到文本型导致测试前提不成立；
# 语料目录来自 config.json 的 roots，不写死个人路径。
# 想固定样本就用 DOC2MD_SMOKE_PDFS（os.pathsep 分隔绝对路径）。
def _resolve_samples() -> list[Path]:
    env = os.environ.get("DOC2MD_SMOKE_PDFS", "").strip()
    if env:
        return [Path(x) for x in env.split(os.pathsep) if x.strip()]
    try:
        from devkit import find_scanned_pdfs
    except ImportError:
        return []
    return find_scanned_pdfs(count=3, max_mb=10.0)


SAMPLES = _resolve_samples()


class _Tee:
    def __init__(self, stream, path: Path):
        self._stream = stream
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", encoding="utf-8")

    def write(self, s):
        self._stream.write(s)
        self._file.write(s)
        return len(s)

    def flush(self):
        self._stream.flush()
        self._file.flush()


def _ensure_real_config() -> bool:
    """config.json 不存在时用 config.example.json 生成一份（fresh clone 场景）。

    本测试从真实 config.json 派生临时配置，所以它得先存在。
    """
    if REAL_CONFIG.exists():
        return True
    try:
        from doc2md.config import bootstrap_config
    except ImportError:
        return False
    return bootstrap_config(REAL_CONFIG) is not None


def build_test_config(tmp: Path) -> Path:
    data = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    src_dir = tmp / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    for s in SAMPLES:
        p = Path(s)
        if p.exists():
            shutil.copy2(p, src_dir / p.name)

    data["roots"] = [str(src_dir)]
    data["output"] = {"mode": "custom", "root": str(tmp / "out"), "layout": "mirror"}
    data["state_db"] = str(tmp / "state.db")
    data["log_dir"] = str(tmp / "logs")

    ocr = data.setdefault("ocr", {})
    ocr["enabled"] = True
    ocr["pdf_trust_check"] = True
    # 关键词：把"坏后端"放在链路最前面，逼出熔断切换
    ocr["backends"] = [
        {
            "name": "broken-paddle",
            "type": "paddle",
            "priority": 0,
            "enabled": True,
            "base_url": "http://127.0.0.1:9",   # 无人监听，必连不通
            "token": "deliberately-wrong",
            "concurrency": 1,
            "max_retries": 1,
            "backpressure_retries": 1,
            "backpressure_base_wait": 0.2,
            "backpressure_max_wait": 0.5,
            "breaker_cooldown": 120.0,
        },
        {
            "name": "mineru-agent",
            "type": "mineru",
            "mode": "agent",
            "priority": 1,
            "enabled": True,
            "base_url": "https://mineru.net",
            "language": "ch",
            "is_ocr": True,
            "enable_table": True,
            "enable_formula": True,
            "max_pages": 20,
            "max_file_mb": 10,
            "chunk_over_limit": False,
            "concurrency": 2,
            "poll_interval": 4.0,
            "job_timeout": 900.0,
            "breaker_cooldown": 180.0,
        },
    ]
    data["local_ocr"] = dict(data.get("local_ocr") or {})
    data["local_ocr"]["enabled"] = True
    data["local_ocr"]["prefer_cloud"] = True
    data["local_ocr"]["local_fallback_on_cloud_down"] = False

    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout

    from doc2md.cli import _banner
    from doc2md.config import load_config
    from doc2md.engine import Engine
    from doc2md.state import StateStore

    print("=" * 74)
    print("  引擎端到端冒烟：真实服务上的熔断切换")
    print("=" * 74)

    if not SAMPLES:
        print("[跳过] 没找到可用的扫描件 PDF 样例。")
        print("       请把 config.json 的 roots 指向真实语料目录，")
        print("       或用 DOC2MD_SMOKE_PDFS 指定（os.pathsep 分隔多个路径）。")
        return 2

    failures = 0
    # ignore_cleanup_errors：Windows 下 state.db 句柄偶尔还没完全释放，
    # 不让清理失败盖掉真正的测试结论（下面仍会显式 close）。
    with tempfile.TemporaryDirectory(prefix="doc2md_smoke_",
                                     ignore_cleanup_errors=True) as d:
        tmp = Path(d)
        cfg_path = build_test_config(tmp)
        cfg = load_config(cfg_path)
        _banner(cfg, "冒烟测试")

        store = StateStore(cfg.state_db)
        eng = Engine(cfg, store, verbose=True, logger=print)
        print(f"路由器后端链路：{eng.ocr.names if eng.ocr else None}")
        print(f"线程池规模    ：local={cfg.raw.get('local_concurrency')} "
              f"ocr={eng.ocr.pool_size if eng.ocr else '-'}")
        # 凭据隔离自检：Paddle 的 token 绝不能串到 MinerU 后端上
        for _b, _c in (eng.ocr.backends if eng.ocr else []):
            _tok = getattr(_c.cfg, "token", "")
            _sends = _c._use_token() if hasattr(_c, "_use_token") else bool(_tok)
            print(f"  后端 {_b.name:<14} type={_b.type:<7} "
                  f"token={'有' if _tok else '无'} 会发 Authorization={'是' if _sends else '否'}")
        print()

        t0 = time.time()
        rep = eng.run(dry_run=False, limit=0)

        print()
        print("-" * 74)
        print("结果汇总")
        print(f"  成功 {rep.ok}  跳过 {rep.skipped}  失败 {rep.failed}  "
              f"拦截 {rep.blocked}  暂缓 {rep.deferred}")
        print(f"  云端 OCR {rep.ocr_files} 个 / {rep.ocr_pages} 页  耗时 {rep.elapsed:.0f}s")
        print(f"  后端用量 {dict(rep.ocr_by_backend)}")
        print(f"  熔断切换 {rep.ocr_switched} 个文件")
        print(f"  插图缺失 {rep.ocr_missing_images} 个文件")
        print(f"  按引擎 {dict(rep.by_engine)}")
        if rep.errors:
            print(f"  失败明细：{rep.errors}")

        print()
        print("后端熔断状态：")
        for st in (eng.ocr.status() if eng.ocr else []):
            print(f"  {st['name']:<16} state={st['state']:<10} "
                  f"reason={st['reason'] or '-':<12} "
                  f"remaining={st['remaining']:.0f}s  pages_today={st['pages_today']}")

        print()
        print("产出的 md：")
        out_root = Path(cfg.output.root)
        mds = sorted(out_root.rglob("*.md"))
        for m in mds:
            assets = m.with_suffix(".assets")
            n_img = len([x for x in assets.rglob("*") if x.is_file()]) if assets.exists() else 0
            text = m.read_text(encoding="utf-8-sig")
            ph = text.count("<!-- image-->") + text.count("<!--image-->")
            print(f"  {m.name[:60]:<62} {len(text):>6} 字  {n_img} 图  占位 {ph}")
        print()

        # ---- 断言 ----
        def check(name: str, cond: bool, detail: str = "") -> None:
            nonlocal failures
            if cond:
                print(f"  [PASS] {name}")
            else:
                failures += 1
                print(f"  [FAIL] {name}  {detail}")

        print("-" * 74)
        print("断言")
        # 注意：BreakerSnapshot 只有 state/fail_count/reason，累计失败次数在熔断器本体上
        n_workers = eng.ocr.pool_size if eng.ocr else 0
        brc = eng.ocr.breakers["broken-paddle"] if eng.ocr else None
        br = brc.snapshot() if brc else None
        check("坏后端已熔断", br is not None and br.state == "open",
              f"state={br.state if br else None} reason={br.reason if br else None}")
        check("熔断原因是 network（连不通），不是误报成 backpressure",
              br is not None and br.reason == "network",
              f"reason={br.reason if br else None}")
        # 并发 worker 可能都已先拿到放行名额再失败，所以上限就是 worker 数
        check(f"熔断后不再对坏后端发新请求（total_failures ≤ {n_workers}）",
              brc is not None and brc.total_failures <= n_workers,
              f"total_failures={brc.total_failures if brc else None}")
        check("备用后端未被无谓熔断（凭据隔离生效）",
              eng.ocr is not None and eng.ocr.breakers["mineru-agent"].state != "open",
              f"state={eng.ocr.breakers['mineru-agent'].state if eng.ocr else None}")
        check("有文件由备用后端接手", rep.ocr_switched >= 1, f"switched={rep.ocr_switched}")
        check("OCR 结果全部来自 mineru-agent",
              set(rep.ocr_by_backend) <= {"mineru-agent"},
              str(dict(rep.ocr_by_backend)))
        check("至少产出 1 份 md", len(mds) >= 1)
        check("失败数为 0", rep.failed == 0, str(rep.errors[:2]))
        good = [m for m in mds if len(m.read_text(encoding='utf-8-sig')) > 100]
        check("产出的 md 有实质内容", len(good) >= 1,
              f"{[(m.name, len(m.read_text(encoding='utf-8-sig'))) for m in mds]}")

        # 熔断器复测：冷却期内再跑一个文件，不该再碰坏后端
        before = brc.total_failures if brc else 0
        ocr_tasks = [t for t in eng.iter_files()]
        if ocr_tasks:
            t = eng.plan(ocr_tasks[0])
            if t is not None:
                with store._lock:  # noqa: SLF001  强制重跑以观察路由
                    store._conn.execute("DELETE FROM files WHERE path = ?", (str(t.src),))
                    store._conn.commit()
                eng.process(t)
        after = eng.ocr.breakers["broken-paddle"].total_failures
        check("熔断期内坏后端零请求", after == before, f"{before} → {after}")

        print()
        print(f"  总耗时 {time.time() - t0:.0f}s")
        eng.close()
        store.close()

    print()
    print("=" * 74)
    print(f"  冒烟测试失败项：{failures}")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
