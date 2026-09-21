"""状态层：SQLite 记录转换结果，实现幂等跳过与断点续传，并统计 OCR 页数配额。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime    REAL    NOT NULL,
    status   TEXT    NOT NULL,
    engine   TEXT,
    md_path  TEXT,
    pages    INTEGER DEFAULT 0,
    error    TEXT,
    updated  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_engine ON files(engine);

CREATE TABLE IF NOT EXISTS usage (
    day   TEXT PRIMARY KEY,
    pages INTEGER NOT NULL DEFAULT 0
);

-- 每个云端 OCR 后端各自的当日用量。usage 表仍记全口径合计（供 status 展示），
-- 这张表用于配额预检：PaddleOCR 的 2 万页额度不该被 MinerU 的消耗挤占。
CREATE TABLE IF NOT EXISTS backend_usage (
    day     TEXT NOT NULL,
    backend TEXT NOT NULL,
    pages   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, backend)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- 幂等判定 ----------
    def is_up_to_date(self, path: str | Path, size: int, mtime: float) -> bool:
        """同一路径、大小与修改时间都一致、且上次转换成功 → 无需重转。"""
        p = str(path)
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime, status FROM files WHERE path = ?", (p,)
            ).fetchone()
        if not row:
            return False
        if row["status"] != "ok":
            return False
        return int(row["size"]) == int(size) and abs(float(row["mtime"]) - float(mtime)) < 1e-6

    def get(self, path: str | Path) -> dict[str, Any] | None:
        p = str(path)
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE path = ?", (p,)).fetchone()
        return dict(row) if row else None

    def md_path_of(self, path: str | Path) -> str | None:
        """本源文件上一次被分配到的 md 路径。用于让归属「粘住」：
        转过的文件永远写回同一个 md，重转只覆盖、不改名。"""
        p = str(path)
        with self._lock:
            row = self._conn.execute(
                "SELECT md_path FROM files WHERE path = ?", (p,)
            ).fetchone()
        if not row:
            return None
        md = row["md_path"]
        return md or None

    def owner_of_md(self, md_path: str | Path) -> str | None:
        """反过来查：这个 .md 是哪个源文件生成的。用于检测输出路径冲突。

        用 lower() 比较：SQLite 的 `=` 走 BINARY 排序规则，盘符大小写不同的同一路径
        会被当成两条不同记录，同一条查不出来就会误判成「无人认领」而改文件名。
        """
        p = str(md_path)
        with self._lock:
            row = self._conn.execute(
                "SELECT path FROM files WHERE lower(md_path) = lower(?) AND status = 'ok' LIMIT 1",
                (p,),
            ).fetchone()
        return row["path"] if row else None

    # ---------- 写入结果 ----------
    def mark(
        self,
        path: str | Path,
        size: int,
        mtime: float,
        status: str,
        engine: str = "",
        md_path: str = "",
        pages: int = 0,
        error: str = "",
    ) -> None:
        p = str(path)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO files (path, size, mtime, status, engine, md_path, pages, error, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size, mtime=excluded.mtime, status=excluded.status,
                    engine=excluded.engine, md_path=excluded.md_path, pages=excluded.pages,
                    error=excluded.error, updated=excluded.updated
                """,
                (p, int(size), float(mtime), status, engine, md_path, int(pages), error, time.time()),
            )
            self._conn.commit()

    def mark_ok(self, path, size, mtime, engine, md_path, pages: int = 0) -> None:
        self.mark(path, size, mtime, "ok", engine, md_path, pages, "")

    def mark_failed(self, path, size, mtime, engine, error: str) -> None:
        self.mark(path, size, mtime, "failed", engine, "", 0, str(error)[:500])

    def mark_skipped(self, path, size, mtime, engine, reason: str) -> None:
        self.mark(path, size, mtime, "skipped", engine, "", 0, str(reason)[:500])

    def mark_deferred(self, path, size, mtime, engine, reason: str) -> None:
        """暂缓：本轮没做成但文件本身没问题（如服务端拥塞、当日配额用尽）。

        状态不是 ok，因此下次重跑会自动再试。
        """
        self.mark(path, size, mtime, "deferred", engine, "", 0, str(reason)[:500])

    def retry_paths(self) -> list[str]:
        """需要重试的文件：真失败 + 上轮暂缓的，按时间顺序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM files WHERE status IN ('failed','deferred') "
                "ORDER BY updated"
            ).fetchall()
        return [r["path"] for r in rows]

    def deferred_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM files WHERE status='deferred'"
            ).fetchone()
        return int(row["c"]) if row else 0

    # ---------- OCR 配额 ----------
    @staticmethod
    def today() -> str:
        return time.strftime("%Y-%m-%d")

    def pages_used_today(self, backend: str = "") -> int:
        """今日已用页数。

        backend 为空 → 全口径合计（`usage` 表，所有后端之和）；
        指定 backend → 该后端自己今日的用量（`backend_usage` 表），用于配额预检。
        """
        with self._lock:
            if backend:
                row = self._conn.execute(
                    "SELECT pages FROM backend_usage WHERE day = ? AND backend = ?",
                    (self.today(), backend),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT pages FROM usage WHERE day = ?", (self.today(),)
                ).fetchone()
        return int(row["pages"]) if row else 0

    def add_pages(self, n: int, backend: str = "") -> int:
        """累加已消耗页数。两本账同时记：全口径 `usage` + 分后端 `backend_usage`。"""
        if n <= 0:
            return self.pages_used_today(backend)
        day = self.today()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usage (day, pages) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET pages = pages + excluded.pages
                """,
                (day, int(n)),
            )
            if backend:
                self._conn.execute(
                    """
                    INSERT INTO backend_usage (day, backend, pages) VALUES (?, ?, ?)
                    ON CONFLICT(day, backend) DO UPDATE SET pages = pages + excluded.pages
                    """,
                    (day, backend, int(n)),
                )
            self._conn.commit()
        return self.pages_used_today(backend)

    def backend_usage_today(self) -> dict[str, int]:
        """今日各后端用量，供 status / 自检展示。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT backend, pages FROM backend_usage WHERE day = ? ORDER BY pages DESC",
                (self.today(),),
            ).fetchall()
        return {r["backend"]: int(r["pages"]) for r in rows}

    # ---------- 统计 ----------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
            by_status = {
                r["status"]: r["c"]
                for r in self._conn.execute(
                    "SELECT status, COUNT(*) c FROM files GROUP BY status"
                )
            }
            by_engine = {
                (r["engine"] or "-"): r["c"]
                for r in self._conn.execute(
                    "SELECT engine, COUNT(*) c FROM files GROUP BY engine ORDER BY c DESC"
                )
            }
            pages = self._conn.execute("SELECT COALESCE(SUM(pages),0) p FROM files").fetchone()["p"]
            failed = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT path, error FROM files WHERE status='failed' ORDER BY updated DESC LIMIT 30"
                )
            ]
        return {
            "total": total,
            "by_status": by_status,
            "by_engine": by_engine,
            "pages_total": pages,
            "pages_today": self.pages_used_today(),
            "backend_pages_today": self.backend_usage_today(),
            "recent_failures": failed,
        }

    # ---------- 重试支持 ----------
    def failed_paths(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM files WHERE status='failed' ORDER BY updated"
            ).fetchall()
        return [r["path"] for r in rows]

    def reset_failed(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM files WHERE status IN ('failed','deferred')"
            )
            self._conn.commit()
        return cur.rowcount

    def reset_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
