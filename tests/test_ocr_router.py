# -*- coding: utf-8 -*-
"""多后端 OCR 路由器的策略测试（用假后端，不联网、不耗配额）。

覆盖的判定表：

  异常类型              熔断该后端   切换下一后端
  -------------------  ----------  ------------
  QuotaExceeded           是           是
  AuthError               是           是
  BackpressureExhausted   是           是
  NetworkUnavailable      是           是
  CapabilityError         否           是
  DocumentError           否           否（快速失败）
  EmptyResultError        否           是
  OcrError（未知）         否           是

另外覆盖熔断器状态机：closed → open → half_open → closed/open（冷却翻倍），
以及配置层"跨厂商后端不继承彼此 Token/服务地址"的隔离（曾导致 mineru-agent 401）。

以及凭据文件（.env）的解析、注入优先级与"改了文件立刻生效"。

以及通用 VLM 后端（OpenAI 兼容 /chat/completions）：输出清洗、端点归一化、
错误分类、厂商级凭据隔离、请求体参数、finish_reason=length 截断告警。

用法：python test_ocr_router.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# 仓库根 = doc2md 包的上一级（脚本位于 <root>/tests 或 <root>/scripts）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 隔离真实凭据：单元测试不得读到生产 .env，否则断言结果取决于本机 Token 填没填
os.environ["DOC2MD_ENV_FILE"] = "none"

LOG_PATH = Path(__file__).with_name("test_ocr_router.log")
_log_file = None


class _Tee:
    """同时写终端和 UTF-8 日志文件。

    本机 PowerShell 管道会把 python 的 UTF-8 输出按 GBK 解码，得到双重编码乱码，
    所以日志必须由 python 自己写文件，不能靠 shell 重定向。
    """

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

    def close(self):
        self._file.close()


def _setup_log() -> None:
    global _log_file
    _log_file = _Tee(sys.stdout, LOG_PATH)
    sys.stdout = _log_file
    sys.stderr = _log_file

from doc2md.config import OcrConfig                                  # noqa: E402
from doc2md.ocr import (                                             # noqa: E402
    AuthError,
    BackendUnavailable,
    BackpressureExhausted,
    CapabilityError,
    DocumentError,
    EmptyResultError,
    NetworkUnavailable,
    OcrError,
    OcrResult,
    QuotaExceeded,
)
from doc2md.ocr_router import (                                      # noqa: E402
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    OcrRouter,
)

_PASS = 0
_FAIL = 0
_LOGS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


# ---------------- 假后端 ----------------
class FakeBackend:
    """按脚本动作的假 OCR 后端。behavior 可以是异常实例/返回值/callable。"""

    type = "fake"

    def __init__(self, name: str, behavior, cfg: OcrConfig):
        self.name = name
        self.cfg = cfg
        self.behavior = behavior
        self.calls = 0
        self.store = None

    def ready(self):
        return True, "就绪"

    def ping(self):
        return True, "假后端"

    def close(self):
        pass

    def convert_file(self, src, md_path, pages=1, batch_id=None):
        self.calls += 1
        act = self.behavior
        if callable(act):
            act = act(self.calls)
        if isinstance(act, BaseException):
            raise act
        return OcrResult(markdown="# 假的\n\n内容\n", pages=pages, image_count=0,
                         elapsed=0.0, job_id=f"job{self.calls}")


def make_cfg(name: str, **kw) -> OcrConfig:
    base = dict(name=name, type="paddle", concurrency=2, breaker_enabled=True,
                breaker_cooldown=300.0, enabled=True, priority=0)
    base.update(kw)
    return OcrConfig(**base)


def make_router(*specs, **kw):
    """specs: (name, behavior, cfg_overrides)"""
    entries = []
    fakes = {}
    for i, (name, behavior, over) in enumerate(specs):
        cfg = make_cfg(name, priority=over.pop("priority", i), **over)
        fake = FakeBackend(name, behavior, cfg)
        entries.append((cfg, fake))
        fakes[name] = fake
    router = OcrRouter(entries, verbose=False, logger=_LOGS.append)
    return router, fakes


_TMP: Path | None = None


def run_case(router, tmpdir: Path | None = None, pages: int = 5):
    d = tmpdir or _TMP
    assert d is not None
    return router.convert_file(d / "src.pdf", d / "out.md", pages=pages)


# ---------------- 测试 ----------------
def test_policies(tmp: Path) -> None:
    section("策略 1：配额耗尽 → 熔断主后端并切换")
    router, fakes = make_router(
        ("A", QuotaExceeded("A 配额用尽"), {}),
        ("B", None, {}),
    )
    res = run_case(router, tmp)
    check("切换到 B 并成功", res.backend == "B")
    check("A 被调用 1 次", fakes["A"].calls == 1)
    check("B 被调用 1 次", fakes["B"].calls == 1)
    check("A 已熔断", router.breakers["A"].snapshot().state == OPEN)
    check("配额熔断=本次运行内不再尝试",
          router.breakers["A"].snapshot().open_until == float("inf"))
    # 第二个文件：A 不应再被调用
    router.convert_file(tmp / "src.pdf", tmp / "out2.md", pages=5)
    check("后续文件不再尝试已熔断的 A", fakes["A"].calls == 1,
          f"A.calls={fakes['A'].calls}")
    check("后续文件继续走 B", fakes["B"].calls == 2)
    check("attempts 记录了链路", res.attempts == ["A", "B"],
          f"实际 attempts={res.attempts!r} backend={res.backend!r}")

    section("策略 2：鉴权失败 → 熔断并切换")
    router, fakes = make_router(
        ("A", AuthError("A Token 无效"), {}),
        ("B", None, {}),
    )
    res = run_case(router)
    check("切换到 B", res.backend == "B")
    check("A 永久熔断", router.breakers["A"].snapshot().open_until == float("inf"))

    section("策略 3：背压耗尽 → 熔断（有冷却）并切换")
    router, fakes = make_router(
        ("A", BackpressureExhausted("队列已满"), {}),
        ("B", None, {}),
    )
    res = run_case(router)
    snap = router.breakers["A"].snapshot()
    check("切换到 B", res.backend == "B")
    check("A 熔断中", snap.state == OPEN)
    check("冷却为 300s（可恢复）", 0 < snap.open_until - time.time() <= 301)

    section("策略 4：能力不足 → 只跳过，不熔断")
    router, fakes = make_router(
        ("A", None, {"max_pages": 10}),
        ("B", None, {}),
    )
    res = run_case(router, tmp, pages=50)
    check("切换到 B", res.backend == "B")
    check("A 完全没被调用", fakes["A"].calls == 0)
    check("A 熔断器仍关闭", router.breakers["A"].snapshot().state == CLOSED)

    section("策略 5：体积超限 → 只跳过，不熔断")
    big = tmp / "big.bin"
    big.write_bytes(b"0" * (3 * 1024 * 1024))
    router, fakes = make_router(
        ("small", None, {"max_file_mb": 1.0}),
        ("big", None, {"max_file_mb": 10.0}),
    )
    res = router.convert_file(big, tmp / "o.md", pages=2)
    check("跳到容量够的后端", res.backend == "big")
    check("小容量后端未被调用", fakes["small"].calls == 0)
    check("小容量后端未熔断", router.breakers["small"].snapshot().state == CLOSED)

    section("策略 6：文档自身问题 → 快速失败，不切换")
    router, fakes = make_router(
        ("A", DocumentError("文件损坏"), {}),
        ("B", None, {}),
    )
    err = None
    try:
        run_case(router)
    except DocumentError as e:
        err = e
    check("抛 DocumentError", err is not None)
    check("备用后端一次都没试（省配额）", fakes["B"].calls == 0)
    check("A 未熔断", router.breakers["A"].snapshot().state == CLOSED)

    section("策略 7：空结果 → 允许换后端再试，但不熔断")
    router, fakes = make_router(
        ("A", EmptyResultError("内容为空"), {}),
        ("B", None, {}),
    )
    res = run_case(router)
    check("切换到 B", res.backend == "B")
    check("A 未熔断", router.breakers["A"].snapshot().state == CLOSED)

    section("策略 8：未知 OcrError → 不熔断但允许切换")
    router, fakes = make_router(
        ("A", OcrError("说不清的错"), {}),
        ("B", None, {}),
    )
    res = run_case(router)
    check("切换到 B", res.backend == "B")
    check("A 未熔断", router.breakers["A"].snapshot().state == CLOSED)

    section("策略 9：全部后端不可用 → 归并为 defer 语义")
    router, fakes = make_router(
        ("A", QuotaExceeded("A 配额用尽"), {}),
        ("B", BackpressureExhausted("B 队列满"), {}),
    )
    err = None
    try:
        run_case(router)
    except BackpressureExhausted as e:
        err = e
    check("抛 BackpressureExhausted（引擎判 deferred）", err is not None,
          repr(err))
    check("两个后端都试过了", fakes["A"].calls == 1 and fakes["B"].calls == 1)

    section("策略 10：全部后端能力不足 → 归并为 CapabilityError")
    router, fakes = make_router(
        ("A", None, {"max_pages": 5}),
        ("B", None, {"max_pages": 8}),
    )
    err = None
    try:
        run_case(router, tmp, pages=100)
    except CapabilityError as e:
        err = e
    check("抛 CapabilityError（文件被判失败，不会反复重试）", err is not None)
    check("两个后端都没被调用", fakes["A"].calls == 0 and fakes["B"].calls == 0)

    section("策略 11：单后端失败（无备用）也要给出明确结局")
    router, fakes = make_router(("only", QuotaExceeded("配额用尽"), {}))
    err = None
    try:
        run_case(router)
    except BackpressureExhausted as e:
        err = e
    check("单后端退化行为正确", err is not None and fakes["only"].calls == 1)

    section("策略 12：网络故障 → 熔断并切换，原因单列为 network")
    router, fakes = make_router(
        ("A", NetworkUnavailable("连不通"), {}),
        ("B", None, {}),
    )
    res = run_case(router)
    snap = router.breakers["A"].snapshot()
    check("切换到 B", res.backend == "B")
    check("A 熔断原因为 network（不再误报 backpressure）",
          snap.reason == "network", f"reason={snap.reason!r}")
    check("A 熔断中且有冷却（可恢复）",
          snap.state == OPEN and 0 < snap.open_until - time.time() <= 301)

    section("策略 13：全后端网络故障 → 归并为 defer 语义（不是 failed）")
    router, fakes = make_router(
        ("A", NetworkUnavailable("A 连不通"), {}),
        ("B", NetworkUnavailable("B 连不通"), {}),
    )
    err = None
    try:
        run_case(router)
    except BackpressureExhausted as e:
        err = e
    check("抛 BackpressureExhausted（引擎判 deferred，留待重跑）",
          err is not None, repr(err))
    check("两个后端都试过了", fakes["A"].calls == 1 and fakes["B"].calls == 1)

    section("策略 14：网络故障 + 能力不足混合 → 仍归并为 defer")
    router, fakes = make_router(
        ("A", NetworkUnavailable("A 连不通"), {}),
        ("B", None, {"max_pages": 3}),
    )
    err = None
    try:
        run_case(router, tmp, pages=50)
    except BackpressureExhausted as e:
        err = e
    check("先试 A（网络故障）再跳过 B（能力不足）",
          fakes["A"].calls == 1 and fakes["B"].calls == 0)
    check("归并为 defer 语义", err is not None, repr(err))


def test_breaker() -> None:
    section("熔断器状态机")
    b = CircuitBreaker("t", enabled=True, fail_threshold=1, cooldown=0.4)
    check("初始为 closed", b.state == CLOSED)
    allowed, kind, why = b.allows()
    check("closed 放行", allowed)

    b.on_failure(BackpressureExhausted("队列满"))
    check("一次背压即熔断", b.state == OPEN)
    allowed, kind, why = b.allows()
    check("open 拒放行", not allowed and kind == "backpressure", why)
    check("查询状态不消耗试探名额", not b.peek()[0])

    time.sleep(0.45)
    check("冷却到期转 half_open", b.state == HALF_OPEN)
    allowed, _, why = b.allows()
    check("half_open 放行一个试探", allowed, why)
    allowed2, _, why2 = b.allows()
    check("试探进行中第二次被拒（防惊群）", not allowed2, why2)

    b.on_success()
    check("试探成功回到 closed", b.state == CLOSED)
    check("失败计数清零", b.snapshot().fail_count == 0)

    # 半开试探失败 → 冷却翻倍
    cooldown_total = []
    b2 = CircuitBreaker("t2", enabled=True, fail_threshold=1, cooldown=0.4)
    b2.on_failure(BackpressureExhausted("队列满"))
    cooldown_total.append(b2.snapshot().open_until - time.time())
    time.sleep(0.45)
    check("b2 进入 half_open", b2.state == HALF_OPEN)
    b2.allows()
    b2.on_failure(BackpressureExhausted("还是满"))
    second = b2.snapshot().open_until - time.time()
    check("半开再失败 → 冷却翻倍", second > cooldown_total[0] * 1.5,
          f"{cooldown_total[0]:.2f}s → {second:.2f}s")

    # 配额类固定永久
    b3 = CircuitBreaker("t3", enabled=True, fail_threshold=1, cooldown=300.0)
    b3.on_failure(QuotaExceeded("配额用尽"))
    check("配额耗尽忽略默认冷却，永久熔断",
          b3.snapshot().open_until == float("inf"))

    # 不熔断的异常类型
    b4 = CircuitBreaker("t4", enabled=True, fail_threshold=1, cooldown=300.0)
    b4.on_failure(CapabilityError("页数超限"))
    b4.on_failure(DocumentError("文件损坏"))
    b4.on_failure(OcrError("未知"))
    check("能力/文档/未知异常都不熔断", b4.state == CLOSED)

    # 关闭熔断开关
    b5 = CircuitBreaker("t5", enabled=False, fail_threshold=1, cooldown=300.0)
    b5.on_failure(QuotaExceeded("配额用尽"))
    check("breaker_enabled=False 时永不熔断", b5.state == CLOSED)

    # 阈值
    b6 = CircuitBreaker("t6", enabled=True, fail_threshold=3, cooldown=300.0)
    b6.on_failure(BackpressureExhausted("1"))
    b6.on_failure(BackpressureExhausted("2"))
    check("未达阈值不熔断", b6.state == CLOSED)
    b6.on_failure(BackpressureExhausted("3"))
    check("达阈值才熔断", b6.state == OPEN)


def test_mixed_chain(tmp: Path) -> None:
    section("三后端链路：A 配额尽 → B 能力不足 → C 接手")
    router, fakes = make_router(
        ("A", QuotaExceeded("A 配额用尽"), {}),
        ("B", None, {"max_pages": 3}),
        ("C", None, {}),
    )
    res = run_case(router, tmp, pages=40)
    check("最终由 C 完成", res.backend == "C")
    check("A 试过 1 次", fakes["A"].calls == 1)
    check("B 因页数超限一次未试", fakes["B"].calls == 0)
    check("B 未熔断", router.breakers["B"].snapshot().state == CLOSED)

    section("统计口径")
    st = {s["name"]: s for s in router.status()}
    check("status 含全部后端", set(st) == {"A", "B", "C"}, str(set(st)))
    check("A 状态 open", st["A"]["state"] == OPEN)
    check("C 状态 closed", st["C"]["state"] == CLOSED)
    check("all_unavailable 仍为 False（C 可用）", not router.all_unavailable())
    check("40 页的大文件只有 C 能接（A 熔断、B 页数超限）",
          router.unavailable(0.0, 40) == ["C"], str(router.unavailable(0.0, 40)))
    router.breakers["C"].on_failure(QuotaExceeded("C 也用尽"))
    check("C 熔断后，1 页的小文件仍可由 B 接手",
          router.unavailable(0.0, 1) == ["B"], str(router.unavailable(0.0, 1)))
    router.breakers["B"].on_failure(QuotaExceeded("B 也用尽"))
    check("全部熔断后 all_unavailable 为 True", router.all_unavailable())


def test_chunk_planning() -> None:
    """MinerU 的 page_ranges 分段：页数与体积双约束。"""
    section("分段规划（MinerU precision：≤200 页 / ≤200MB，开分段）")
    from doc2md.mineru import MinerUOcrClient

    cfg = make_cfg("mu", type="mineru", mode="precision", max_pages=200,
                   max_file_mb=200.0, chunk_over_limit=True, token="x")
    st = type("S", (), {"pages_used_today": lambda self, b="": 0,
                        "add_pages": lambda self, n, backend="": 0})()
    cli = MinerUOcrClient(cfg, store=st, verbose=False, logger=lambda m: None)

    check("未超限时单段", cli._plan_chunks(150, 100.0) == [(1, 150, None)],
          str(cli._plan_chunks(150, 100.0)))

    c = cli._plan_chunks(500, 100.0)
    check("页数超限 → 按 200 页切 3 段", c == [(1, 200, "1-200"),
                                              (201, 400, "201-400"),
                                              (401, 500, "401-500")], str(c))

    # 229 页 / 420MB：页数没超但体积超，应按体积折算
    c = cli._plan_chunks(229, 420.0)
    check("体积超限也能切（229 页/420MB）", len(c) >= 2, str(c))
    check("每段页数按体积折算而非 200",
          all(b - a + 1 <= 200 for a, b, _ in c) and len(c) >= 2, str(c))
    check("分段范围连续且完整覆盖",
          c[0][0] == 1 and c[-1][1] == 229
          and all(c[i][1] + 1 == c[i + 1][0] for i in range(len(c) - 1)), str(c))

    cfg2 = make_cfg("mu2", type="mineru", mode="agent", max_pages=20,
                    max_file_mb=10.0, chunk_over_limit=False, token="")
    cli2 = MinerUOcrClient(cfg2, store=st, verbose=False, logger=lambda m: None)
    err = None
    try:
        cli2._plan_chunks(100, 3.0)
    except CapabilityError as e:
        err = e
    check("未开分段 → 超限抛 CapabilityError", err is not None)
    check("未超限正常单段（agent 20 页内）",
          cli2._plan_chunks(18, 3.0) == [(1, 18, None)])


def test_config_isolation() -> None:
    """跨厂商后端不能继承彼此的 Token / 服务地址。

    回归：ocr 段是 PaddleOCR（token 非空），backends 里的 mineru-agent 没写 token，
    于是继承了 Paddle 的 token 并发给 MinerU → 401 A0202 → 备用通道被永久熔断。
    """
    import json
    from doc2md.config import load_config
    from doc2md.mineru import MinerUOcrClient

    section("配置：跨厂商后端不继承凭据")
    with tempfile.TemporaryDirectory() as d:
        data = {
            "ocr": {
                "enabled": True,
                "type": "paddle",
                "token": "PADDLE-TOKEN-XYZ",
                "base_url": "https://paddle.example.com",
                "backends": [
                    {"name": "p1", "type": "paddle", "priority": 0},
                    {"name": "m1", "type": "mineru", "mode": "agent", "priority": 1},
                    {"name": "m2", "type": "mineru", "mode": "precision", "priority": 2,
                     "token": "MINERU-TOKEN-ABC"},
                ],
            },
            "roots": [], "output": {"mode": "custom", "root": str(Path(d) / "out")},
            "state_db": str(Path(d) / "s.db"), "log_dir": str(Path(d) / "logs"),
        }
        p = Path(d) / "c.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cfg = load_config(p)

    base_token = cfg.ocr.token
    by_name = {b.name: b for b in cfg.ocr_backends}
    check("链路按 priority 排序",
          [b.name for b in cfg.ocr_backends] == ["p1", "m1", "m2"],
          str([b.name for b in cfg.ocr_backends]))
    check("同厂商后端继承 ocr 段的 Token",
          by_name["p1"].token == base_token and bool(base_token),
          f"p1.token={by_name['p1'].token!r} base={base_token!r}")
    check("跨厂商后端 Token 不被继承（关键回归）",
          by_name["m1"].token == "", f"m1.token={by_name['m1'].token!r}")
    check("跨厂商后端服务地址换成本类型默认值",
          by_name["m1"].base_url == "https://mineru.net",
          f"m1.base_url={by_name['m1'].base_url!r}")
    check("显式写的 Token 生效（不被默认值覆盖）",
          by_name["m2"].token == "MINERU-TOKEN-ABC", f"m2.token={by_name['m2'].token!r}")

    # ---- Authorization 发送策略 ----
    section("MinerU：agent 默认不发 Authorization，precision 发")
    c_agent = make_cfg("ma", type="mineru", mode="agent", token="SOME-TOKEN")
    c_prec = make_cfg("mp", type="mineru", mode="precision", token="SOME-TOKEN")
    c_none = make_cfg("mn", type="mineru", mode="agent", token="")
    cli_agent = MinerUOcrClient(c_agent, verbose=False, logger=lambda m: None)
    cli_prec = MinerUOcrClient(c_prec, verbose=False, logger=lambda m: None)
    cli_none = MinerUOcrClient(c_none, verbose=False, logger=lambda m: None)
    check("agent + 有 token → 仍不发（文档：无需 Authorization）",
          cli_agent._use_token() is False)
    check("precision + 有 token → 发",
          cli_prec._use_token() is True)
    check("无 token → 不发", cli_none._use_token() is False)
    check("agent 的请求头里确实没有 Authorization",
          "Authorization" not in cli_agent._headers(),
          str(cli_agent._headers()))
    check("precision 的请求头里有 Authorization",
          cli_prec._headers().get("Authorization") == "Bearer SOME-TOKEN")
    check("agent 模式不需要 Token 即视为就绪",
          cli_none.ready()[0] is True, str(cli_none.ready()))

    # send_token 显式覆盖
    c_force = make_cfg("mf", type="mineru", mode="agent", token="T", send_token=True)
    cli_force = MinerUOcrClient(c_force, verbose=False, logger=lambda m: None)
    check("send_token=True 可强制发送（自建代理场景）",
          cli_force._use_token() is True)


class _EnvGuard:
    """测试期间对 os.environ 的改动在退出时全部回滚（含新增键与 config 的内部记账）。"""

    KEYS = (
        "DOC2MD_ENV_FILE", "DOC2MD_MINERU_TOKEN", "MINERU_TOKEN",
        "DOC2MD_MINERU_BASE_URL", "DOC2MD_PADDLE_TOKEN",
        "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN",
        "DOC2MD_TOKEN_P1", "DOC2MD_TOKEN_MP", "PROBE_A", "PROBE_B", "PROBE_Q",
        # VLM 相关（厂商级 / 兜底 / 精确指定）
        "DOC2MD_SILICONFLOW_TOKEN", "SILICONFLOW_API_KEY", "DOC2MD_VLM_TOKEN",
        "DOC2MD_DASHSCOPE_TOKEN", "DASHSCOPE_API_KEY", "DOC2MD_VLM_BASE_URL",
        "DOC2MD_TOKEN_SF", "DOC2MD_TOKEN_DBL",
    )

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from doc2md import config as _c
        _c._ENV_FILE_KEYS.difference_update(self.KEYS)   # noqa: SLF001
        return False


def test_env_file(tmp: Path) -> None:
    """凭据文件（.env）：解析、注入优先级、热刷新、按后端指定。"""
    import json

    from doc2md import config as cfgmod
    from doc2md.config import (
        _parse_env_text,
        load_config,
        load_env_file,
        mask_token,
    )

    section("凭据文件：解析")

    parsed = _parse_env_text(
        "# 整行注释\n"
        "\n"
        "  ; 分号注释也认\n"
        "PROBE_A=1\n"
        "export PROBE_B = 2  # 行尾注释\n"
        'PROBE_Q="a # b"\n'
        "没等号的行跳过\n"
        "=只有值没有键也跳过\n"
    )
    check("普通键值", parsed.get("PROBE_A") == "1", repr(parsed.get("PROBE_A")))
    check("export 前缀 + 等号两边空格 + 行尾注释",
          parsed.get("PROBE_B") == "2", repr(parsed.get("PROBE_B")))
    check("引号内的 # 不当注释",
          parsed.get("PROBE_Q") == "a # b", repr(parsed.get("PROBE_Q")))
    check("无等号/无键名的行被跳过", "没等号的行跳过" not in parsed and len(parsed) == 3,
          str(parsed))

    section("凭据文件：注入与优先级")
    env_path = tmp / "creds.env"
    env_path.write_text(
        "# 凭据\nDOC2MD_MINERU_TOKEN=FILE-MINERU\nPROBE_A=from-file\n",
        encoding="utf-8-sig",          # 故意带 BOM，模拟记事本
    )
    with _EnvGuard():
        os.environ["PROBE_A"] = "from-real-env"
        os.environ.pop("DOC2MD_MINERU_TOKEN", None)
        path, applied = load_env_file(env_path)
        check("能读到带 BOM 的文件", path == env_path, str(path))
        check("新键被注入 os.environ",
              os.environ.get("DOC2MD_MINERU_TOKEN") == "FILE-MINERU")
        check("已存在的真实环境变量不被覆盖（外部优先）",
              os.environ.get("PROBE_A") == "from-real-env")
        check("返回值只列本次真正写入的键",
              "PROBE_A" not in applied and "DOC2MD_MINERU_TOKEN" in applied,
              str(applied))

        # 改了文件要立刻生效（否则改完 .env 必须重启才认，体验很差）
        env_path.write_text("DOC2MD_MINERU_TOKEN=FILE-MINERU-V2\nPROBE_A=from-file\n",
                            encoding="utf-8")
        load_env_file(env_path)
        check("同一进程内改动文件后重新读取即生效",
              os.environ.get("DOC2MD_MINERU_TOKEN") == "FILE-MINERU-V2",
              str(os.environ.get("DOC2MD_MINERU_TOKEN")))

        # 空值＝占位行，不覆盖任何东西
        env_path.write_text("DOC2MD_MINERU_TOKEN=\n", encoding="utf-8")
        load_env_file(env_path)
        check("空值不覆盖已有值（占位行安全）",
              os.environ.get("DOC2MD_MINERU_TOKEN") == "FILE-MINERU-V2")

        # 关掉开关
        os.environ["DOC2MD_ENV_FILE"] = "none"
        check("DOC2MD_ENV_FILE=none 时不读文件",
              load_env_file() == (None, {}))
        os.environ["DOC2MD_ENV_FILE"] = str(env_path)
        check("DOC2MD_ENV_FILE 可指向任意路径",
              load_env_file()[0] == env_path)

    section("Token 脱敏显示")
    check("空 Token", mask_token("") == "(空)")
    check("短 Token 全部打码（不泄露长度以外的信息）",
          "*" in mask_token("abc") and "abc" not in mask_token("abc"),
          mask_token("abc"))
    masked = mask_token("6839d8332e5b3e963b485101ee4e567e8cbf5e58")
    check("长 Token 只留头尾", masked.startswith("6839") and "…" in masked
          and "2e5b" not in masked, masked)

    section("凭据文件 → 各后端（端到端注入）")
    with _EnvGuard():
        env_path.write_text(
            "DOC2MD_MINERU_TOKEN=MU-FROM-FILE\n"
            "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN=PD-FROM-FILE\n",
            encoding="utf-8",
        )
        os.environ["DOC2MD_ENV_FILE"] = str(env_path)
        data = {
            "ocr": {
                "enabled": True, "type": "paddle", "base_url": "https://p.ai",
                "backends": [
                    {"name": "p1", "type": "paddle", "priority": 0},
                    {"name": "mp", "type": "mineru", "mode": "precision", "priority": 1},
                ],
            },
            "roots": [], "output": {"mode": "custom", "root": str(tmp / "out")},
            "state_db": str(tmp / "s2.db"), "log_dir": str(tmp / "logs"),
        }
        cpath = tmp / "c2.json"
        cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cfg = load_config(cpath)
        by = {b.name: b for b in cfg.ocr_backends}
        check("cfg.env_file 记录了实际读取的凭据文件", cfg.env_file == env_path,
              str(cfg.env_file))
        check("cfg.env_keys 记录生效键名",
              sorted(cfg.env_keys) == ["DOC2MD_MINERU_TOKEN",
                                       "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN"],
              str(cfg.env_keys))
        check("PaddleOCR 后端从文件取到 Token", by["p1"].token == "PD-FROM-FILE",
              repr(by["p1"].token))
        check("MinerU 后端从文件取到 Token", by["mp"].token == "MU-FROM-FILE",
              repr(by["mp"].token))
        check("凭据来源可追溯",
              "env/.env" in cfgmod.credential_source(by["mp"]),
              cfgmod.credential_source(by["mp"]))

    section("按后端名精确指定 Token（多账号场景）")
    with _EnvGuard():
        os.environ.pop("DOC2MD_MINERU_TOKEN", None)
        os.environ["DOC2MD_TOKEN_MP"] = "MP-SPECIFIC"
        data["ocr"]["backends"] = [
            {"name": "mp", "type": "mineru", "mode": "precision", "priority": 0,
             "token": "CONFIG-TOKEN"},
            {"name": "mp2", "type": "mineru", "mode": "precision", "priority": 1},
        ]
        cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cfg = load_config(cpath)
        by = {b.name: b for b in cfg.ocr_backends}
        check("DOC2MD_TOKEN_<后端名> 覆盖 config.json 里的 Token",
              by["mp"].token == "MP-SPECIFIC", repr(by["mp"].token))
        check("未被精确指定的后端不受影响", by["mp2"].token == "",
              repr(by["mp2"].token))
        check("来源显示为精确键名",
              "DOC2MD_TOKEN_MP" in cfgmod.credential_source(by["mp"]),
              cfgmod.credential_source(by["mp"]))


def test_vlm_client(tmp: Path) -> None:
    """通用 VLM（OpenAI 兼容）客户端：离线校验，不联网、不耗配额。

    重点验三件容易出错的：
      ① 输出清洗（grounding 标签、\\boxed{} 外壳）—— 不清就满篇乱码标签
      ② 厂商级凭据匹配 —— DOC2MD_SILICONFLOW_TOKEN 绝不能灌进别家（百炼）后端
      ③ finish_reason=length 的截断告警 —— 不检查就是静默丢内容
    """
    import json

    from doc2md import config as cfgmod
    from doc2md.config import load_config
    from doc2md.ocr_router import build_router
    from doc2md.vlm import (
        PROMPT_DOC2MD,
        VlmOcrClient,
        clean_vlm_markdown,
    )

    section("VLM 输出清洗")
    raw = (
        "<|ref|>关于加强安全监管的通知<|/ref|>"
        "<|det|>[[100,50,800,90]]<|/det|>\n"
        "各单位：\n"
        r"\boxed{\begin{tabular}{cc}a & b \\ c & d\end{tabular}}" "\n"
        "<|ref|>附件<|/ref|><|det|>[[10,900,80,930]]<|/det|>\n"
        "<image>\n"
    )
    cleaned = clean_vlm_markdown(raw)
    check("grounding 定位标签被清掉",
          "<|ref|>" not in cleaned and "<|det|>" not in cleaned
          and "<|grounding|>" not in cleaned, cleaned[:80])
    check("坐标块被整块删除", "100,50" not in cleaned)
    check("定位标签里的文字保留", "关于加强安全监管的通知" in cleaned)
    check("boxed 外壳被剥掉、内容保留（含嵌套花括号）",
          r"\boxed{" not in cleaned and "tabular" in cleaned, cleaned[:120])
    check("回吐的 <image> 被去掉", "<image>" not in cleaned)

    section("VLM 端点归一化")
    cli = VlmOcrClient(make_cfg("t", type="vlm", token="K",
                                base_url="https://api.siliconflow.cn/v1"))
    check("base_url 已带 /v1 时不重复拼",
          cli._endpoint("/chat/completions")
          == "https://api.siliconflow.cn/v1/chat/completions", cli._endpoint("/chat/completions"))
    cli2 = VlmOcrClient(make_cfg("t2", type="vlm", token="K",
                                 base_url="https://api.siliconflow.cn"))
    check("base_url 只有域名时自动补 /v1（否则 404）",
          cli2._endpoint("/chat/completions")
          == "https://api.siliconflow.cn/v1/chat/completions", cli2._endpoint("/chat/completions"))

    section("VLM 就绪判定")
    ok, why = VlmOcrClient(make_cfg("a", type="vlm", token="")).ready()
    check("缺 Token → 不纳入链路，且提示去哪填",
          ok is False and ".env" in why, why)
    ok, _ = VlmOcrClient(make_cfg("b", type="vlm", token="K")).ready()
    check("有 Token → 就绪", ok is True)
    ok, why = VlmOcrClient(make_cfg("c", type="vlm", token="",
                                    send_token=False)).ready()
    check("显式 send_token=False → 允许免鉴权自建服务",
          ok is True, why)

    section("VLM 错误分类")
    v = VlmOcrClient(make_cfg("v", type="vlm", token="K"))
    e = v._classify_business(50505, "Model service overloaded.", "识别")
    check("50505 服务过载 → 背压（可熔断可切换、留待重跑）",
          isinstance(e, BackpressureExhausted), type(e).__name__)
    e = v._classify_business(20012, "model not found: deepseek-ai/DeepSeek-OCR", "识别")
    check("模型名不存在 → 后端不可用", isinstance(e, BackendUnavailable),
          type(e).__name__)
    check("模型名不存在 → 冷却 0（本次运行内永久熔断，不再白撞）",
          e is not None and getattr(e, "cooldown", None) == 0.0,
          str(getattr(e, "cooldown", None)))
    e = v._classify_http(404, None, "404 page not found", "识别")
    check("404 视为配置错误（base_url 写错）→ 永久熔断",
          isinstance(e, BackendUnavailable) and getattr(e, "cooldown", None) == 0.0,
          type(e).__name__)
    check("背压业务码已登记", v._backpressure_codes() == {50505},
          str(v._backpressure_codes()))

    section("VLM 请求体与截断检测（假 HTTP，不联网）")

    class _FakeResp:
        def __init__(self, payload, status=200, text=""):
            self._p, self.status_code, self.text = payload, status, text

        def json(self):
            return self._p

    class _FakeSess:
        def __init__(self, resp):
            self.resp, self.last_json, self.last_headers = resp, None, None

        def post(self, url, headers=None, json=None, timeout=None):
            self.last_json, self.last_headers = json, headers
            return self.resp

    def _with_sess(payload, behavior=None):
        c = VlmOcrClient(make_cfg("s", type="vlm", token="TOK", max_pages=2,
                                  max_tokens=1234, temperature=0.0,
                                  chunk_over_limit=True))
        sess = _FakeSess(_FakeResp(payload))
        c._local.sess = sess
        return c, sess

    c, sess = _with_sess({
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "# 标题\n\n正文"}}],
        "usage": {"total_tokens": 42},
    })
    text, usage, cut = c._chat([{"type": "text", "text": "x"}], "测试")
    check("正常响应取到 content", "正文" in text and usage.get("total_tokens") == 42)
    check("截断标记为 False", cut is False)
    check("强制 stream=False（硅基流动流式返回格式异常）",
          sess.last_json.get("stream") is False, str(sess.last_json.get("stream")))
    check("temperature=0 保证转录确定性",
          sess.last_json.get("temperature") == 0.0)
    check("max_tokens 按配置下发（不传平台会报错）",
          sess.last_json.get("max_tokens") == 1234)
    check("Authorization 头带上 Token",
          (sess.last_headers or {}).get("Authorization") == "Bearer TOK")

    c, _ = _with_sess({
        "choices": [{"finish_reason": "length",
                     "message": {"content": "# 被切一半"}}],
        "usage": {},
    })
    text, _, cut = c._chat([{"type": "text", "text": "x"}], "测试")
    check("finish_reason=length → 标记为截断", cut is True)
    check("截断会在正文留下显式告警（否则事后无从判断）",
          "截断" in text, text[-60:])

    section("VLM 单文件转换（假 HTTP，非 PDF 走图片模式）")
    png = tmp / "scan.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    store = type("S", (), {
        "pages_used_today": lambda self, b="": 0,
        "add_pages": lambda self, n, backend="": None,
    })()
    c, sess = _with_sess({
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "<!-- image-->\n\n# 扫描件\n\n正文一段"}}],
        "usage": {"total_tokens": 7},
    })
    c.store = store
    c.cfg.input_mode = "image"
    res = c.convert_file(png, tmp / "scan.md", pages=1)
    check("返回 OcrResult 且后端名正确", res.backend == "s", res.backend)
    check("md 内容落到结果里", "扫描件" in res.markdown, res.markdown[:60])
    check("不返回插图（image_count=0）", res.image_count == 0)
    check("job_id 带上请求数与 token 便于审计",
          "req" in res.job_id and "tok" in res.job_id, res.job_id)
    check("走 image 模式时下发的是 image/png data URL",
          str(sess.last_json["messages"][0]["content"][0]["image_url"]["url"])
          .startswith("data:image/png;base64,"),
          str(sess.last_json["messages"][0]["content"][0])[:80])

    section("VLM 输入构造（真 PDF：切段 / 光栅化，不联网）")
    import pymupdf

    pdf = tmp / "three.pdf"
    _d = pymupdf.open()
    for i in range(3):
        _pg = _d.new_page(width=595, height=842)
        _pg.insert_text((72, 100), f"Page {i + 1}", fontsize=20)
    _d.save(str(pdf))
    _d.close()

    cp = VlmOcrClient(make_cfg("p", type="vlm", token="K",
                               input_mode="pdf", dpi=72))
    blob = cp._slice_pdf(pdf, 1, 2)
    check("切段产出合法 PDF（能再打开）", blob[:5] == b"%PDF-", blob[:8])
    with pymupdf.open(stream=blob, filetype="pdf") as _chk:
        check("切段页数正确（取 1-2 页 → 2 页）", _chk.page_count == 2,
              str(_chk.page_count))
    with pymupdf.open(stream=cp._slice_pdf(pdf, 3, 3), filetype="pdf") as _chk:
        check("单页切段也正确", _chk.page_count == 1, str(_chk.page_count))

    err = None
    try:
        cp._slice_pdf(pdf, 9, 10)
    except DocumentError as e:
        err = e
    check("超出实际页数 → DocumentError（文件问题，不熔断不切换）",
          err is not None, str(err)[:60])

    ci = VlmOcrClient(make_cfg("pi", type="vlm", token="K",
                               input_mode="image", dpi=72))
    imgs = ci._page_images(pdf, 1, 2)
    check("光栅化出 2 张图", len(imgs) == 2, str(len(imgs)))
    check("图是合法 PNG（不是空字节）",
          all(b[:8] == b"\x89PNG\r\n\x1a\n" for b in imgs)
          and all(len(b) > 200 for b in imgs),
          str([len(b) for b in imgs]))

    section("VLM 厂商级凭据隔离（本轮新增的坑）")
    data = {
        "ocr": {
            "enabled": True,
            "backends": [
                {"name": "sf", "type": "vlm", "priority": 0,
                 "base_url": "https://api.siliconflow.cn/v1",
                 "model": "deepseek-ai/DeepSeek-OCR",
                 "returns_images": False, "max_pages": 2,
                 "chunk_over_limit": True},
                {"name": "dbl", "type": "vlm", "priority": 1,
                 "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "model": "qwen-vl-ocr",
                 "returns_images": False, "max_pages": 1,
                 "chunk_over_limit": True},
            ],
        },
        "roots": [],
        "output": {"mode": "custom", "root": str(tmp / "out3")},
        "state_db": str(tmp / "s3.db"),
        "log_dir": str(tmp / "logs3"),
    }
    cpath = tmp / "c3.json"
    with _EnvGuard():
        for k in ("DOC2MD_SILICONFLOW_TOKEN", "SILICONFLOW_API_KEY",
                  "DOC2MD_VLM_TOKEN", "DOC2MD_DASHSCOPE_TOKEN", "DASHSCOPE_API_KEY",
                  "DOC2MD_TOKEN_SF", "DOC2MD_TOKEN_DBL"):
            os.environ.pop(k, None)
        os.environ["DOC2MD_SILICONFLOW_TOKEN"] = "SF-TOKEN"
        cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cfg = load_config(cpath)
        by = {b.name: b for b in cfg.ocr_backends}
        check("硅基流动后端取到自家 Token", by["sf"].token == "SF-TOKEN",
              repr(by["sf"].token))
        check("**百炼后端不会被灌进硅基流动 Token**（会 401 并永久熔断）",
              by["dbl"].token == "", repr(by["dbl"].token))
        check("凭据来源指向正确的键名",
              "DOC2MD_SILICONFLOW_TOKEN" in cfgmod.credential_source(by["sf"]),
              cfgmod.credential_source(by["sf"]))

        # 只想给某一家时用通用兜底键（不参与厂商匹配）
        os.environ.pop("DOC2MD_SILICONFLOW_TOKEN", None)
        os.environ["DOC2MD_VLM_TOKEN"] = "ANY-VLM"
        cfg = load_config(cpath)
        by = {b.name: b for b in cfg.ocr_backends}
        check("DOC2MD_VLM_TOKEN 发给所有 vlm 后端",
              by["sf"].token == "ANY-VLM" and by["dbl"].token == "ANY-VLM",
              f"{by['sf'].token}/{by['dbl'].token}")

        os.environ.pop("DOC2MD_VLM_TOKEN", None)
        os.environ["DOC2MD_TOKEN_SF"] = "SF-SPECIFIC"
        cfg = load_config(cpath)
        by = {b.name: b for b in cfg.ocr_backends}
        check("DOC2MD_TOKEN_<后端名> 优先级最高",
              by["sf"].token == "SF-SPECIFIC" and by["dbl"].token == "",
              f"{by['sf'].token}/{by['dbl'].token}")

        section("VLM 挂进路由器")
        os.environ.pop("DOC2MD_TOKEN_SF", None)
        os.environ["DOC2MD_SILICONFLOW_TOKEN"] = "SF-TOKEN"
        cfg = load_config(cpath)
        router = build_router(cfg, store=None, verbose=False, logger=_LOGS.append)
        check("缺 Token 的 vlm 后端被排除、有 Token 的进链路",
              router is not None and router.names == ["sf"],
              str(router.names if router else None))
        check("returns_images 如实上报（引擎据此提示插图会缺）",
              router is not None and router.returns_images("sf") is False)
        check("链路描述里标出『无插图』",
              "无插图" in cfgmod.describe_backends(cfg),
              cfgmod.describe_backends(cfg))
        if router is not None:
            st = router.status()
            check("status 暴露 returns_images 供界面显示",
                  st and st[0].get("returns_images") is False, str(st[:1]))


def main() -> int:
    _setup_log()
    print("=" * 74)
    print("  多后端云端 OCR 熔断切换 —— 策略测试")
    print("=" * 74)
    with tempfile.TemporaryDirectory() as d:
        global _TMP
        tmp = Path(d)
        _TMP = tmp
        src = tmp / "src.pdf"
        src.write_bytes(b"%PDF-1.4 fake\n" * 10)
        test_policies(tmp)
        test_breaker()
        test_mixed_chain(tmp)
        test_chunk_planning()
        test_config_isolation()
        test_env_file(tmp)
        test_vlm_client(tmp)

    print()
    print("=" * 74)
    print(f"  通过 {_PASS}   失败 {_FAIL}")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
