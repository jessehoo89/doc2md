"""实时监控：watchdog 监听新增/修改，带写入防抖与自触发保护。"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import (
    Config,
    is_excluded,
    is_in_output_root,
    load_config,
    resolve_config_path,
)
from .engine import Engine
from .state import StateStore

_IGNORE_SUFFIX = {".md", ".db", ".log", ".tmp", ".temp", ".crdownload", ".part", ".swp"}
_IGNORE_PREFIX = ("~$", ".~", "._")

# 支持热加载的字段：改这些不用重启。OCR 相关参数改动仍需重启。
_HOT_FIELDS = (
    "roots",
    "exclude_dir_names",
    "sensitive_markers",
    "watch_extensions",
    "image_extensions",
    "debounce_seconds",
    "keep_original",
    "overwrite_existing_md",
    "output",
    "min_text_chars_per_page",
    "text_pdf_probe_pages",
    "shield_sensitive_for_ocr",
    "pdf_use_layout",
    "max_excel_rows",
    "max_excel_cols",
    "excel_sheet_limit",
)
_CONFIG_POLL = 2.0


class _Handler(FileSystemEventHandler):
    def __init__(self, cfg: Config, sink: "PendingQueue"):
        self.cfg = cfg
        self.sink = sink

    def _consider(self, raw_path: str, kind: str) -> None:
        p = Path(raw_path)
        if p.suffix.lower() in _IGNORE_SUFFIX:
            return
        if p.name.startswith(_IGNORE_PREFIX):
            return
        if p.suffix.lower() not in self.cfg.watch_ext_set:
            return
        if is_excluded(self.cfg, p):
            return
        if is_in_output_root(self.cfg, p):
            return
        if self.sink.is_own_output(p):
            return
        self.sink.touch(p, kind)

    def on_created(self, event):
        if not event.is_directory:
            self._consider(str(event.src_path), "新增")

    def on_modified(self, event):
        if not event.is_directory:
            self._consider(str(event.src_path), "修改")

    def on_moved(self, event):
        if not event.is_directory:
            self._consider(str(event.dest_path), "移入")


class PendingQueue:
    """收集事件 → 等文件写入稳定 → 交给引擎。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pending: dict[str, tuple[float, int, str]] = {}
        self._lock = threading.Lock()
        self._own: set[str] = set()
        self._q: queue.Queue[Path] = queue.Queue()

    def touch(self, p: Path, kind: str) -> None:
        try:
            size = p.stat().st_size
        except OSError:
            return
        key = str(p).lower()
        with self._lock:
            self._own.discard(key)
            self._pending[key] = (time.time(), size, kind)

    def is_own_output(self, p: Path) -> bool:
        with self._lock:
            return str(p).lower() in self._own

    def mark_own(self, p: Path) -> None:
        with self._lock:
            self._own.add(str(p).lower())

    def clear(self) -> None:
        """丢弃待处理队列（配置变更时调用）。"""
        with self._lock:
            self._pending.clear()

    def drain_ready(self) -> list[Path]:
        """取出已稳定的文件。"""
        now = time.time()
        ready: list[Path] = []
        with self._lock:
            for key in list(self._pending):
                t, size, _kind = self._pending[key]
                if now - t < self.cfg.debounce_seconds:
                    continue
                p = Path(key)
                try:
                    cur = p.stat().st_size
                except OSError:
                    self._pending.pop(key, None)
                    continue
                if cur != size or cur == 0:
                    # 还在写入，刷新时间戳继续等
                    self._pending[key] = (now, cur, self._pending[key][2])
                    continue
                self._pending.pop(key, None)
                ready.append(p)
        return ready


class WatchService:
    def __init__(self, cfg: Config, store: StateStore, engine: Engine,
                 verbose: bool = True, logger=None, config_path=None):
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self.verbose = verbose
        self.log = logger or (lambda m: print(m, flush=True))
        self.pending = PendingQueue(cfg)
        self._observer: Observer | None = None
        self._stop = threading.Event()
        self._work_q: queue.Queue = queue.Queue()
        self._config_path = resolve_config_path(config_path)
        self._cfg_mtime: float = self._read_mtime()
        self._watched_roots: list[str] = []

    # ---------- 配置热加载 ----------
    def _read_mtime(self) -> float:
        try:
            return self._config_path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self) -> None:
        """配置文件一变就重载。就地改字段，让 engine / handler 里的引用同步生效。"""
        mtime = self._read_mtime()
        if mtime == 0.0 or mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            new = load_config(self._config_path)
        except Exception as e:
            self.log(f"[配置] 重载失败，继续用旧配置：{e}")
            return

        old_roots = [str(Path(r)) for r in self.cfg.roots]
        for f in _HOT_FIELDS:
            setattr(self.cfg, f, getattr(new, f))
        # 排除目录等字段常被原地追加/移除，同步清一遍待处理队列
        self.pending.clear()

        self.log("[配置] 已重载配置")
        self.log(f"        处理目录: {', '.join(self.cfg.roots)}")

        new_roots = [str(Path(r)) for r in self.cfg.roots]
        if new_roots != old_roots:
            self.log("        目录变化，正在重建监控…")
            self._schedule_observers()
        else:
            self.log("        OCR 等参数改动需重启才生效")

    def _schedule_observers(self) -> None:
        """按当前 roots 重建监控。"""
        if self._observer is not None:
            try:
                self._observer.unschedule_all()
            except Exception:
                pass
        else:
            self._observer = Observer()
            self._observer.start()

        handler = _Handler(self.cfg, self.pending)
        self._watched_roots = []
        for root in self.cfg.roots:
            rp = Path(root)
            if not rp.exists():
                self.log(f"[警告] 监控目录不存在：{root}")
                continue
            self._observer.schedule(handler, str(rp), recursive=True)
            self._watched_roots.append(str(rp))
        if self._watched_roots:
            self.log(f"已监控：{'，'.join(self._watched_roots)}")

    # ---------- 后台转换线程 ----------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                p = self._work_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                t = self.engine.plan(p)
                if t is None:
                    continue
                status, info = self.engine.process(t)
                md = self.engine.md_path_for(p)
                self.pending.mark_own(md)
                self.pending.mark_own(md.with_suffix(".assets"))
                icon = {"ok": "✓", "skip": "·", "fail": "✗", "blocked": "⊘"}[status]
                self.log(f"{icon} [{info}] {p.name}")
            except Exception as e:
                self.log(f"✗ [监控异常] {p.name}: {type(e).__name__}: {e}")
            finally:
                self._work_q.task_done()

    # ---------- 启动 ----------
    def start(self, catch_up: bool = True) -> None:
        if catch_up:
            self.log("启动前先做一次全量扫描（补齐历史未转文件）…")
            self.engine.run(dry_run=False)

        self._observer = Observer()
        self._observer.start()
        self._schedule_observers()
        if not self._watched_roots:
            self.log("[错误] 没有任何可监控的目录，请检查 config.json 的 roots。")
            self.stop()
            return

        workers = int(self.cfg.raw.get("watch_workers", 2))
        for i in range(max(1, workers)):
            threading.Thread(target=self._worker, daemon=True, name=f"d2m-w{i}").start()

        self.log(f"\n监控中（防抖 {self.cfg.debounce_seconds}s，{workers} 个转换线程）。")
        self.log(f"配置文件热加载已开启，改 {self._config_path.name} 无需重启。按 Ctrl+C 退出。\n")

        last_check = 0.0
        try:
            while not self._stop.is_set():
                for p in self.pending.drain_ready():
                    self._work_q.put(p)
                now = time.time()
                if now - last_check >= _CONFIG_POLL:
                    last_check = now
                    self._maybe_reload()
                time.sleep(0.5)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        self.engine.close()
        self.log("监控已停止。")
