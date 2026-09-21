"""多后端云端 OCR 路由器：按优先级尝试，遇故障熔断并自动切换。

## 为什么需要它

单个云端 OCR 服务并不总是可用。2026-09-21 那次全量重转实测踩到三种：

  1. 服务端背压 —— PaddleOCR 返 `code=10010 任务提交队列已满`，连续 3 个文件
     都撞上后，引擎只能把剩余 183 个文件全部标 `deferred`，白等一轮（69 分钟
     只做完 136/311）。
  2. 当日配额耗尽 —— 撞上配额后再试也是白费，但当时的实现会一个个文件死磕。
  3. 我方配额计数 bug —— 误判"配额用尽"，实际还剩 6,265 页。

只要有第二个通道，上面三种都不会让整轮停摆。

## 三层结构

  ┌── 路由器 OcrRouter ───────────────────────────────────────┐
  │  按 priority 顺序遍历后端，逐个做三件事：                  │
  │    ① 熔断器是否放行（open 就跳过，half_open 放一个试探）    │
  │    ② 能力是否匹配（页数/体积超限就跳过，不算故障）          │
  │    ③ 真正转换，成功即返回；失败按异常类型决定"熔断/切换/失败"│
  │  全部后端走完仍失败 → 归并成 QuotaExceeded /                │
  │  BackpressureExhausted / CapabilityError，交引擎定性        │
  └───────────────────────────────────────────────────────────┘

## 熔断器状态机

      ┌─────────┐  失败达阈值（trip=True 的异常）   ┌───────┐
      │ closed  │ ───────────────────────────────→ │ open  │
      │ 正常放行 │                                   │ 全拒  │
      └─────────┘ ←─────────────────────────────── └───────┘
           ↑            半开试探成功                    │
           │                                            │ 冷却到期
           │        ┌────────────┐                      ↓
           └─────── │ half_open  │ ←────────────────────┘
             成功    │ 只放 1 个试探│
                    └────────────┘
                      └─ 试探失败 → 重新 open（冷却时间翻倍，上限 30 分钟）

冷却时间按故障类型取：配额耗尽 / 鉴权失败 = 0（本次运行内不再尝试），
背压 = 300 秒（服务端队列通常几分钟就缓过来），可由配置覆盖。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config, OcrConfig, backend_label, is_sensitive
from .mineru import MinerUOcrClient
from .ocr import (
    AuthError,
    BackendUnavailable,
    BackpressureExhausted,
    CapabilityError,
    DocumentError,
    OcrError,
    OcrResult,
    PaddleOcrClient,
    QuotaExceeded,
)
from .vlm import VlmOcrClient

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# 熔断后冷却时间上限：即使连续试探失败，也不会无限拉长
MAX_COOLDOWN = 1800.0

# 熔断器的"未启用"冷却哨兵：0 表示本次进程内永久 open
FOREVER = float("inf")


@dataclass
class BreakerSnapshot:
    name: str
    state: str
    fail_count: int
    open_until: float
    reason: str = ""

    @property
    def remaining(self) -> float:
        if self.open_until in (0.0, FOREVER):
            return 0.0 if self.open_until == 0.0 else FOREVER
        return max(0.0, self.open_until - time.time())


class CircuitBreaker:
    """单后端的熔断器。

    `trip` 语义来自异常类本身（见 ocr.py）：
      可熔断的只有 BackendUnavailable 家族（配额/鉴权/背压）；
      能力不足与文档自身问题不熔断，因为后端本身是健康的。
    """

    def __init__(self, name: str, enabled: bool = True, fail_threshold: int = 1,
                 cooldown: float = 300.0):
        self.name = name
        self.enabled = enabled
        self.fail_threshold = max(1, int(fail_threshold))
        self.cooldown = max(0.0, float(cooldown))
        self._lock = threading.Lock()
        self._fail_count = 0
        self._open_until = 0.0
        self._probe_running = False
        self._reason = ""
        self._trip_count = 0
        self.total_failures = 0

    # ---------- 查询 ----------
    @property
    def state(self) -> str:
        if not self.enabled:
            return CLOSED
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._open_until == 0.0:
            return CLOSED
        if self._open_until == FOREVER or time.time() < self._open_until:
            return OPEN
        return HALF_OPEN

    def allows(self) -> tuple[bool, str, str]:
        """能否放行一次调用。返回 (放行?, 熔断类型, 原因)。

        注意：HALF_OPEN 状态下**会占用**唯一那个试探名额，所以只应由真正准备
        发请求的调用方使用；纯查询请用 `peek()`。
        """
        if not self.enabled:
            return True, "", ""
        with self._lock:
            st = self._state_locked()
            if st == CLOSED:
                return True, "", ""
            if st == OPEN:
                left = (FOREVER if self._open_until == FOREVER
                        else max(0.0, self._open_until - time.time()))
                tip = "本次运行内不再尝试" if left == FOREVER else f"还剩 {left:.0f}s"
                return False, self._reason or "unavailable", f"熔断中（{tip}）"
            # HALF_OPEN：只放一个线程去试探，避免惊群
            if self._probe_running:
                return False, self._reason or "unavailable", "半开试探进行中"
            self._probe_running = True
            return True, "", "半开试探"

    def peek(self) -> tuple[bool, str, str]:
        """只读查询：是否处于可用状态。不消耗半开试探名额，无副作用。"""
        if not self.enabled:
            return True, "", ""
        with self._lock:
            st = self._state_locked()
            if st == CLOSED:
                return True, "", ""
            if st == OPEN:
                left = (FOREVER if self._open_until == FOREVER
                        else max(0.0, self._open_until - time.time()))
                tip = "本次运行内不再尝试" if left == FOREVER else f"还剩 {left:.0f}s"
                return False, self._reason or "unavailable", f"熔断中（{tip}）"
            return True, "", "半开待试探"

    # ---------- 状态变更 ----------
    def on_success(self) -> None:
        with self._lock:
            if self._open_until != 0.0 and self._state_locked() == HALF_OPEN:
                self._cooldown_reset_locked()
            self._fail_count = 0
            self._open_until = 0.0
            self._reason = ""
            self._probe_running = False

    def on_failure(self, exc: BaseException) -> bool:
        """记录一次失败。返回是否"刚刚熔断"（用于日志）。"""
        trip = bool(getattr(exc, "trip", False))
        with self._lock:
            self._probe_running = False
            if not trip:
                return False
            self._fail_count += 1
            self.total_failures += 1
            kind = str(getattr(exc, "kind", "unavailable"))
            if self._fail_count < self.fail_threshold:
                return False
            cd = getattr(exc, "cooldown", None)
            base = self.cooldown if cd is None else max(0.0, float(cd))
            if self._open_until == FOREVER:
                # 已经永久熔断，无须再加长
                return False
            was_half_open = self._open_until != 0.0
            if was_half_open and base > 0:
                # 半开试探又失败 → 冷却翻倍，避免对坏后端反复试探
                base = min(base * 2, MAX_COOLDOWN)
            self._open_until = FOREVER if base <= 0 else time.time() + base
            self._reason = kind
            self._trip_count += 1
            return True

    def _cooldown_reset_locked(self) -> None:
        self._fail_count = 0

    # ---------- 统计 ----------
    def snapshot(self) -> BreakerSnapshot:
        with self._lock:
            st = self._state_locked()
            return BreakerSnapshot(
                name=self.name,
                state=st,
                fail_count=self._fail_count,
                open_until=self._open_until,
                reason=self._reason,
            )


def capability_reason(b: OcrConfig, size_mb: float, pages: int) -> str | None:
    """该后端能不能处理这个文件（不能则返回原因）。不算故障，不熔断。

    页数或体积超限时，若该后端开了 `chunk_over_limit` 且页数已知，
    就认为"可以分段解决"，不算能力不足 —— 真正的切段由客户端负责。
    """
    over_size = bool(b.max_file_mb and size_mb > b.max_file_mb)
    over_pages = bool(b.max_pages and pages > b.max_pages)
    if not (over_size or over_pages):
        return None
    if b.chunk_over_limit and pages:
        return None
    why = []
    if over_size:
        why.append(f"体积 {size_mb:.1f}MB > {b.max_file_mb:g}MB")
    if over_pages:
        why.append(f"页数 {pages} > {b.max_pages}")
    if b.chunk_over_limit:
        why.append("页数未知无法分段")
    else:
        why.append("未开分段提交")
    return "，".join(why)


class OcrRouter:
    """多云端 OCR 后端路由器。对外接口与单后端客户端一致（convert_file / ping）。"""

    def __init__(self, entries: list[tuple[OcrConfig, Any]], verbose: bool = True,
                 logger: Callable[[str], None] | None = None):
        if not entries:
            raise ValueError("OcrRouter 至少需要一个后端")
        self.verbose = verbose
        self._logger = logger
        self.backends: list[tuple[OcrConfig, Any]] = entries
        self._sems: dict[str, threading.Semaphore] = {
            b.name: threading.Semaphore(max(1, int(b.concurrency or 1)))
            for b, _ in self.backends
        }
        self.breakers: dict[str, CircuitBreaker] = {
            b.name: CircuitBreaker(
                name=b.name,
                enabled=bool(b.breaker_enabled),
                fail_threshold=1,
                cooldown=float(b.breaker_cooldown or 300.0),
            )
            for b, _ in self.backends
        }

    # ---------- 基础信息 ----------
    @property
    def names(self) -> list[str]:
        return [b.name for b, _ in self.backends]

    @property
    def primary(self) -> str:
        return self.names[0]

    @property
    def pool_size(self) -> int:
        """建议的工作线程数：各后端并发之和，让不同后端能并行干活。"""
        return max(1, sum(max(1, int(b.concurrency or 1)) for b, _ in self.backends))

    def backend_cfg(self, name: str) -> OcrConfig | None:
        for b, _ in self.backends:
            if b.name == name:
                return b
        return None

    def returns_images(self, name: str) -> bool:
        """该后端会不会把插图交还。找不到时按 True（不误报）。"""
        b = self.backend_cfg(name)
        return True if b is None else bool(getattr(b, "returns_images", True))

    def _log(self, msg: str) -> None:
        if self._logger is not None:
            self._logger(msg)
        elif self.verbose:
            print(msg, flush=True)

    def __getattr__(self, item: str):
        """未定义属性转发给主后端，兼容只看 `client.cfg` 之类的旧用法。"""
        if item.startswith("_"):
            raise AttributeError(item)
        backends = self.__dict__.get("backends") or []
        if not backends:
            raise AttributeError(item)
        primary_cfg, primary_client = backends[0]
        if hasattr(primary_client, item):
            return getattr(primary_client, item)
        if hasattr(primary_cfg, item):
            return getattr(primary_cfg, item)
        raise AttributeError(item)

    # ---------- 状态 ----------
    def status(self) -> list[dict]:
        out = []
        for b, client in self.backends:
            snap = self.breakers[b.name].snapshot()
            used = 0
            store = getattr(client, "store", None)
            if store is not None:
                try:
                    used = store.pages_used_today(b.name)
                except Exception:
                    used = 0
            out.append({
                "name": b.name,
                "type": b.type,
                "mode": b.mode,
                "model": b.model,
                "priority": b.priority,
                "concurrency": b.concurrency,
                "state": snap.state,
                "reason": snap.reason,
                "remaining": snap.remaining,
                "trips": snap.fail_count,
                "total_failures": self.breakers[b.name].total_failures,
                "max_pages": b.max_pages,
                "max_file_mb": b.max_file_mb,
                "returns_images": bool(getattr(b, "returns_images", True)),
                "daily_page_limit": b.daily_page_limit,
                "pages_today": used,
            })
        return out

    def unavailable(self, size_mb: float, pages: int) -> list[str]:
        """当前既没熔断、能力也匹配的后端名。空列表 = 本轮没法再云端 OCR。

        纯查询语义：用 `peek()` 而非 `allows()`，不会占掉半开试探名额。
        """
        ok = []
        for b, _ in self.backends:
            allowed, _, _ = self.breakers[b.name].peek()
            if not allowed:
                continue
            if capability_reason(b, size_mb, pages):
                continue
            ok.append(b.name)
        return ok

    # ---------- 核心：带熔断切换的转换 ----------
    def convert_file(self, src: Path, md_path: Path, pages: int = 1,
                     batch_id: str | None = None) -> OcrResult:
        try:
            size_mb = src.stat().st_size / 1048576.0
        except OSError:
            size_mb = 0.0
        pages = pages or 1

        failures: list[tuple[str, str, str]] = []   # (后端, 类型, 说明)
        tried: list[str] = []

        for b, client in self.backends:
            allowed, kind, why = self.breakers[b.name].allows()
            if not allowed:
                failures.append((b.name, kind or "unavailable", why))
                continue

            cap = capability_reason(b, size_mb, pages)
            if cap:
                failures.append((b.name, "capability", cap))
                self._log(f"[路由] 跳过 {b.name}：{cap}")
                continue

            if tried:
                self._log(f"[路由] 切换到备用后端 {b.name}"
                          f"（{tried[-1]} 不可用），继续处理 {src.name}")
            tried.append(b.name)
            try:
                with self._sems[b.name]:
                    res = client.convert_file(src, md_path, pages=pages, batch_id=batch_id)
            except DocumentError as e:
                # 文档自身的问题，换后端也是同样结果 → 立刻失败，省下其它后端配额
                self._log(f"[路由] {b.name} 判定文件本身有问题，不再尝试其它后端：{e}")
                raise
            except OcrError as e:
                tripped = self.breakers[b.name].on_failure(e)
                kind = str(getattr(e, "kind", "error"))
                if tripped:
                    snap = self.breakers[b.name].snapshot()
                    left = ("本次运行内不再尝试" if snap.open_until == FOREVER
                            else f"{snap.remaining:.0f}s")
                    self._log(f"[熔断] {b.name} 因 {kind} 熔断（{left}）：{e}")
                if not getattr(e, "failover", True):
                    raise
                failures.append((b.name, kind, str(e)))
                continue
            else:
                self.breakers[b.name].on_success()
                res.backend = b.name
                res.attempts = list(tried)
                return res

        raise self._aggregate(failures, tried, src)

    def _aggregate(self, failures: list[tuple[str, str, str]], tried: list[str],
                   src: Path) -> OcrError:
        """所有后端都没成 —— 归类成引擎认识的那几种结局。"""
        detail = "；".join(f"{n}[{k}] {d}" for n, k, d in failures) or "无可用后端"
        kinds = {k for _, k, _ in failures}
        # 会自己好起来的：配额（明天）、背压（几分钟）、网络（本机/对面恢复）、
        # 通用 unavailable（服务端临时故障）；鉴权换 Token 也能好。
        # 这些都标 deferred 留待重跑，而不是判该文件失败。
        defer_kinds = {"quota", "backpressure", "network", "unavailable"}
        if kinds and kinds <= (defer_kinds | {"capability", "auth"}):
            if kinds & defer_kinds or "auth" in kinds:
                return BackpressureExhausted(
                    f"所有云端 OCR 后端均暂不可用，{src.name} 留待稍后重试：{detail}"
                )
            return CapabilityError(
                f"没有后端能处理 {src.name}（能力/鉴权限制）：{detail}"
            )
        return OcrError(f"{src.name} 所有云端 OCR 后端均失败：{detail}")

    def all_unavailable(self, size_mb: float = 0.0, pages: int = 1) -> bool:
        """所有后端都已熔断或能力不足 —— 引擎据此暂停本轮剩余 OCR。"""
        return not self.unavailable(size_mb, pages)

    # ---------- 自检 ----------
    def ping_all(self) -> list[tuple[str, bool, str]]:
        out = []
        for b, client in self.backends:
            if not b.enabled:
                out.append((b.name, False, "已在配置中禁用"))
                continue
            ready_fn = getattr(client, "ready", None)
            if callable(ready_fn):
                ok, why = ready_fn()
                if not ok:
                    out.append((b.name, False, why))
                    continue
            try:
                ok, msg = client.ping()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {str(e)[:120]}"
            out.append((b.name, ok, msg))
        return out

    def close(self) -> None:
        for _, client in self.backends:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def build_router(cfg: Config, store=None, verbose: bool = True,
                 logger: Callable[[str], None] | None = None) -> OcrRouter | None:
    """按配置装配路由器。返回 None 表示云端 OCR 关闭或没有任何可用后端。"""
    if not cfg.ocr.enabled:
        return None

    def log(msg: str) -> None:
        if logger is not None:
            logger(msg)
        elif verbose:
            print(msg, flush=True)

    entries: list[tuple[OcrConfig, Any]] = []
    disabled: list[str] = []
    for b in sorted(cfg.ocr_backends, key=lambda x: x.priority):
        if not b.enabled:
            disabled.append(f"{b.name}（配置中禁用）")
            continue
        try:
            if b.type == "mineru":
                client = MinerUOcrClient(b, store=store, verbose=verbose, logger=logger)
            elif b.type == "paddle":
                client = PaddleOcrClient(b, store=store, verbose=verbose, logger=logger)
            elif b.type == "vlm":
                client = VlmOcrClient(b, store=store, verbose=verbose, logger=logger)
            else:
                disabled.append(f"{b.name}（未知类型 {b.type}）")
                continue
        except Exception as e:
            disabled.append(f"{b.name}（装配失败：{type(e).__name__}: {e}）")
            continue
        ready_fn = getattr(client, "ready", None)
        if callable(ready_fn):
            ok, why = ready_fn()
            if not ok:
                disabled.append(f"{b.name}（{why}）")
                continue
        entries.append((b, client))

    if disabled:
        log("[路由] 以下后端未纳入链路：" + "，".join(disabled))
    if not entries:
        log("[路由] 没有任何可用的云端 OCR 后端，云端 OCR 将不可用")
        return None
    if len(entries) > 1:
        chain = " → ".join(b.name for b, _ in entries)
        log(f"[路由] 云端 OCR 多后端链路：{chain}"
            f"（失败自动熔断切换，共 {len(entries)} 个后端）")
    return OcrRouter(entries, verbose=verbose, logger=logger)


def sensitive_blocked(cfg: Config, src: Path) -> bool:
    """敏感目录不允许上云（无论有几个后端）。"""
    return is_sensitive(cfg, src)
