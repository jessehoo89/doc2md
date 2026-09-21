"""调度引擎：扫描 → 分流 → 转换 → 归档，含幂等跳过、敏感目录拦截与并发执行。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from . import converters, detect, local_ocr
from .com import ComConverter, cleanup_temp
from .config import Config, is_excluded, is_sensitive
from .detect import Kind
from .ocr import (
    BackendUnavailable,
    BackpressureExhausted,
    DocumentError,
    NetworkUnavailable,
    OcrError,
    PaddleOcrClient,
    QuotaExceeded,
)
from .ocr_router import build_router
from .state import StateStore

# 真实格式 → 处理路线
OFFICE_LOCAL = {Kind.DOCX: "docx", Kind.XLSX: "xlsx"}
OFFICE_COM = {Kind.DOC: "doc", Kind.XLS: "xls"}
SKIP_KINDS = {Kind.MISSING, Kind.UNKNOWN, Kind.ZIP_UNKNOWN, Kind.OLE_UNKNOWN,
              Kind.PPT, Kind.PPTX, Kind.IMAGE, Kind.RTF}


def _norm(p) -> str:
    """路径归一化：统一大小写与分隔符，用于比较「是不是同一个路径」。

    中文 Windows 上 `F:\\` 与 `f:\\`、`a/b` 与 `a\\b` 指的是同一处，直接字符串
    比较会把它们当成两个不同文件，从而误判成「重名需要改名」。
    """
    return os.path.normcase(os.path.normpath(str(p)))


def _same_path(a, b) -> bool:
    return _norm(a) == _norm(b)


@dataclass
class Task:
    src: Path
    size: int
    mtime: float
    kind: Kind
    route: str
    pages: int = 0
    note: str = ""


@dataclass
class Report:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    deferred: int = 0
    ocr_pages: int = 0
    ocr_files: int = 0
    ocr_paused: bool = False
    ocr_switched: int = 0          # 因主后端熔断而切换链路的文件数
    ocr_missing_images: int = 0    # 后端只返回文字、插图丢失的文件数
    by_engine: Counter = field(default_factory=Counter)
    ocr_by_backend: Counter = field(default_factory=Counter)
    errors: list[tuple[str, str]] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_engine(self, name: str) -> None:
        with self.lock:
            self.by_engine[name] += 1

    def add_error(self, path: str, err: str) -> None:
        with self.lock:
            self.errors.append((path, err[:200]))

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


class Engine:
    def __init__(self, cfg: Config, store: StateStore, verbose: bool = True,
                 logger: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.verbose = verbose
        self._log_lock = threading.Lock()
        self._log_fn = logger or (lambda m: print(m, flush=True))
        self.local_ocr = (
            local_ocr.LocalOcrClient(
                cfg.local_ocr.python_exe, device=cfg.local_ocr.device, verbose=verbose
            )
            if cfg.local_ocr.enabled
            else None
        )
        self.com = ComConverter(verbose=False)
        self._com_sem = threading.Semaphore(int(cfg.raw.get("com_concurrency", 2)))
        # 云端 OCR 是多后端路由器：按优先级尝试，遇故障熔断并自动切换备用后端。
        # 只配一个后端时它就等价于单客户端。
        self.ocr = build_router(cfg, store=store, verbose=verbose, logger=self.log)
        self._ocr_sem = threading.Semaphore(
            max(1, self.ocr.pool_size if self.ocr is not None else 1)
        )
        self._seq = 0
        self._seq_lock = threading.Lock()
        # 自定义输出下的路径认领表：md_path(小写) -> 源文件。
        # 多线程并发时，光查状态库会漏判撞名（两边都还没落库），必须内存里先占位。
        self._md_claims: dict[str, str] = {}
        self._claim_lock = threading.Lock()
        # 全局熔断：链路上所有后端都不可用（配额/背压/鉴权/网络）时，
        # 继续逐个文件死磕只会白耗几小时，直接暂停剩余 OCR 留待稍后重跑。
        self._down_streak = 0
        self._ocr_paused = False
        self._down_lock = threading.Lock()
        # 已经提示过"不返回插图"的后端名，避免同一句提示刷满日志
        self._no_image_notified: set[str] = set()
        self.report = Report()

    def log(self, msg: str) -> None:
        with self._log_lock:
            self._log_fn(msg)

    def _tag(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return f"{self._seq:>6}"

    # ---------------- 扫描与分流 ----------------
    def iter_files(self):
        seen: set[str] = set()
        for root in self.cfg.roots:
            rp = Path(root)
            if not rp.exists():
                self.log(f"[警告] 根目录不存在，已跳过：{root}")
                continue
            for dirpath, dirnames, filenames in _walk(rp):
                if is_excluded(self.cfg, dirpath):
                    dirnames[:] = []
                    continue
                for name in filenames:
                    p = Path(dirpath) / name
                    if p.suffix.lower() not in self.cfg.watch_ext_set:
                        continue
                    if p.suffix.lower() in {".md"} or p.name.startswith("~$"):
                        continue
                    key = str(p).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    yield p

    def plan(self, path: Path) -> Task | None:
        """判断单个文件该走哪条路。返回 None 表示跳过。"""
        try:
            st = path.stat()
        except OSError:
            return None
        size, mtime = st.st_size, st.st_mtime
        if size == 0:
            return None

        kind = detect.sniff(path)
        ext = path.suffix.lower()

        if kind in OFFICE_LOCAL:
            return Task(path, size, mtime, kind, OFFICE_LOCAL[kind])
        if kind in OFFICE_COM:
            return Task(path, size, mtime, kind, OFFICE_COM[kind])
        if kind is Kind.HTML:
            return Task(path, size, mtime, kind, "html")
        if kind is Kind.RTF:
            return Task(path, size, mtime, kind, "rtf")
        if kind is Kind.PDF:
            try:
                if self.cfg.pdf_trust_check:
                    # 可信度判断：字数够但文本层是"扫描件 + OCR 层 / 乱码 / 隐形"的，
                    # 一律改走 OCR 重新识别，而不是直接抽这层不可靠的文本。
                    text_ok, pages, why = detect.pdf_text_trust(
                        path, self.cfg.min_text_chars_per_page, self.cfg.text_pdf_probe_pages
                    )
                else:
                    text_ok, pages, _chars = detect.is_text_pdf(
                        path, self.cfg.min_text_chars_per_page, self.cfg.text_pdf_probe_pages
                    )
                    why = ""
            except Exception as e:
                return Task(path, size, mtime, kind, "error", 0, f"PDF 读取失败：{e}")
            if text_ok:
                return Task(path, size, mtime, kind, "pdf_text", pages)
            return Task(path, size, mtime, kind, "pdf_ocr", pages, why)
        if kind is Kind.IMAGE:
            if ext in self.cfg.image_ext_set:
                return Task(path, size, mtime, kind, "img_ocr", 1)
            return None
        # 扩展名像 Office 但内容不认 —— 尝试走 COM 兜底
        if ext in (".doc", ".wps", ".xls", ".et"):
            return Task(path, size, mtime, kind, "com_fallback")
        return None

    def md_path_for(self, src: Path) -> Path:
        """按配置算输出路径。

        自定义输出下可能出现重名：`x.docx` 与 `x.pdf` 都算成 `x.md`（同名不同格式），
        flat 平铺时不同目录的同名文件也会撞。处理策略见 output.on_collision：

        - overwrite（默认）：同名就覆盖，名字永远只有一个，重转只覆盖不改名。
        - stable：先到先得 + 归属粘住 —— 已在状态库里归属过的路径永久保留，
          后到者另存 `x_<hash>.md`（hash 由**源文件路径**决定，所以重转永远同名）。
        - suffix：旧行为。每次重转重新抢一次，抢不到就加 hash，名字会变（不推荐）。

        判定与占位必须在同一把锁里完成，否则并发时两个线程都会认为自己是第一个。
        """
        p = converters.md_path_for(src, self.cfg.output, self.cfg.roots)
        if not self.cfg.output.is_custom:
            return p

        src_key = str(src)
        mode = self.cfg.output.on_collision

        with self._claim_lock:
            # 1) 粘住本源的既有归属：转过的文件永远写回同一个 md
            prev = self.store.md_path_of(src)
            if prev and self._is_same_target(Path(prev), p):
                self._md_claims[_norm(prev)] = src_key
                return Path(prev)

            # 2) 覆盖模式：不加后缀，同名直接覆盖
            if mode == "overwrite":
                self._md_claims[_norm(p)] = src_key
                return p

            # 3) stable / suffix：冲突才加后缀
            key = _norm(p)
            owner = self.store.owner_of_md(p)
            claimant = self._md_claims.get(key)
            taken = bool(owner and not _same_path(owner, src_key)) or bool(
                claimant and not _same_path(claimant, src_key)
            )
            if taken:
                h = hashlib.sha1(src_key.encode("utf-8")).hexdigest()[:6]
                q = p.with_name(f"{p.stem}_{h}{p.suffix}")
                self._md_claims[_norm(q)] = src_key
                return q
            self._md_claims.setdefault(key, src_key)
            return p

    @staticmethod
    def _is_same_target(prev: Path, base: Path) -> bool:
        """prev 是否可视为 base 这个名字的合法归属（含 `x_<hash>.md` 形式的别名）。"""
        if _same_path(prev, base):
            return True
        if _norm(prev.parent) != _norm(base.parent):
            return False
        m = re.fullmatch(r"(.+)_[0-9a-f]{6}", prev.stem, re.I)
        return bool(m) and m.group(1) == base.stem

    # ---------------- 单文件执行 ----------------
    def process(self, t: Task) -> tuple[str, str]:
        """返回 (结果, 引擎/原因)。结果 ∈ ok / skip / fail / blocked。"""
        src = t.src
        md_path = self.md_path_for(src)

        # 幂等：未变化且已成功 → 跳过
        if self.store.is_up_to_date(src, t.size, t.mtime) and md_path.exists():
            return "skip", "已转换"

        # 手工改动过 md（比源文件新）时不覆盖
        if md_path.exists() and self.cfg.overwrite_existing_md == "skip":
            try:
                if md_path.stat().st_mtime > t.mtime:
                    self.store.mark_ok(src, t.size, t.mtime, t.route, str(md_path), t.pages)
                    return "skip", "已有较新 md"
            except OSError:
                pass

        try:
            if t.route == "error":
                raise RuntimeError(t.note)
            if t.route in ("docx", "xlsx", "html", "rtf", "pdf_text", "pdf_ocr", "img_ocr"):
                return self._run_local(t, md_path)
            if t.route in ("doc", "xls", "com_fallback"):
                return self._run_com(t, md_path)
            return "skip", f"未支持的路线 {t.route}"
        except QuotaExceeded as e:
            # 当日配额用尽，明天重跑即可，不算失败
            self.store.mark_deferred(src, t.size, t.mtime, t.route, str(e))
            return "defer", "当日 OCR 配额已用尽"
        except Exception as e:
            self.store.mark_failed(src, t.size, t.mtime, t.route, f"{type(e).__name__}: {e}")
            if self.verbose:
                self.log(f"      ↳ {traceback.format_exc(limit=2).strip().splitlines()[-1]}")
            return "fail", f"{type(e).__name__}: {e}"

    def _assets(self, md_path: Path) -> Path:
        return converters.assets_dir_for(md_path)

    def _write_md(self, md_path: Path, text: str, title: str | None) -> None:
        """写 Markdown —— 目标已存在时是**覆盖**，不另存、不改名。

        覆盖时顺手清掉 `.assets` 里新 md 已不再引用的旧图：重转后插图变了
        （页数减少、图被删），旧文件会永远躺在那里，链接却已不存在。
        """
        existed = md_path.exists()
        converters.write_markdown(md_path, text, title)
        if not existed:
            return
        n = _prune_assets(md_path, text)
        _drop_empty_dir(converters.assets_dir_for(md_path))
        if n and self.verbose:
            self.log(f"      ↳ 覆盖旧 md，清掉 {n} 个已不引用的旧图")

    def _run_local(self, t: Task, md_path: Path) -> tuple[str, str]:
        src = t.src
        title = src.stem
        assets = None

        if t.route == "pdf_ocr" or t.route == "img_ocr":
            return self._run_ocr_with_local(t, md_path)

        if t.route == "docx":
            assets = self._assets(md_path)
            md = converters.docx_to_markdown(src, assets)
            engine = "docx"
        elif t.route == "xlsx":
            md = converters.xlsx_to_markdown(
                src, self.cfg.max_excel_rows, self.cfg.max_excel_cols, self.cfg.excel_sheet_limit
            )
            engine = "xlsx"
        elif t.route == "html":
            md = converters.html_to_markdown(src)
            engine = "html"
        elif t.route == "rtf":
            return self._run_com(t, md_path)
        elif t.route == "pdf_text":
            md = converters.pdf_to_markdown(src, use_layout=self.cfg.pdf_use_layout)
            engine = "pdf-text"
        else:
            raise RuntimeError(f"未知本地路线 {t.route}")

        if not md.strip():
            raise RuntimeError("解析结果为空")
        self._write_md(md_path, md, title)
        _drop_empty_dir(assets)
        self.store.mark_ok(src, t.size, t.mtime, engine, str(md_path), t.pages)
        return "ok", engine

    def _run_com(self, t: Task, md_path: Path) -> tuple[str, str]:
        """老式格式：先经 COM 转 OOXML，再本地解析。"""
        src = t.src
        with self._com_sem:
            tmp = self.com.to_ooxml(src, t.kind.value)
        try:
            if tmp.suffix.lower() == ".docx":
                md = converters.docx_to_markdown(tmp, self._assets(md_path))
                engine = "com-docx"
            else:
                md = converters.xlsx_to_markdown(
                    tmp, self.cfg.max_excel_rows, self.cfg.max_excel_cols, self.cfg.excel_sheet_limit
                )
                engine = "com-xlsx"
        finally:
            cleanup_temp(tmp)

        if not md.strip():
            raise RuntimeError("COM 转换后解析结果为空")
        self._write_md(md_path, md, src.stem)
        if engine == "com-docx":
            _drop_empty_dir(self._assets(md_path))
        self.store.mark_ok(src, t.size, t.mtime, engine, str(md_path), t.pages)
        return "ok", engine

    def _run_ocr_with_local(self, t: Task, md_path: Path) -> tuple[str, str]:
        """扫描件 OCR：本地 RapidOCR 与云端 PaddleOCR-VL 的分流。

        分流策略：
          1. 敏感目录 → 强制本地（合规，绝不上云）；本地不可用则拦截。
          2. 本地可用 + prefer_cloud=False（默认）→ 本地识别 + 启发式表格还原；
             检测到复杂表且允许云兜底 → 转发云端重做（VLM 保真）。
          3. prefer_cloud=True 且非敏感目录 → 先走云端 VLM（版式/表格保真更好），
             云端失败再退回本地。
          4. 本地不可用 → 直接走云端。
        """
        src = t.src
        sensitive = is_sensitive(self.cfg, src)
        local_ok = self.local_ocr is not None
        prefer_cloud = bool(getattr(self.cfg.local_ocr, "prefer_cloud", False))

        # 敏感目录：只能本地，不能上云
        if sensitive and not local_ok:
            self.store.mark_skipped(src, t.size, t.mtime, t.route,
                                    "敏感目录，且本地 OCR 未启用，禁止上传云端")
            return "blocked", "敏感目录已拦截（无本地 OCR）"

        # 优先云端（非敏感目录）
        if prefer_cloud and not sensitive and self.ocr is not None:
            try:
                return self._run_ocr(t, md_path)
            except (BackendUnavailable, DocumentError) as e:
                # 云端整体不可用（配额/背压/鉴权）或文件本身有问题：
                # 默认**不**悄悄降级成本地识别 —— 那会让这批文件以较低质量
                # 被记成 ok，从此再也不会用云端重做。宁可标记 deferred 留待重跑。
                if not getattr(self.cfg.local_ocr, "local_fallback_on_cloud_down", False):
                    raise
                if not local_ok:
                    raise
                self.log(f"      ↳ 云端不可用（{type(e).__name__}: {e}），"
                         f"按配置降级本地 OCR")
            except Exception as e:
                if not local_ok:
                    raise
                self.log(f"      ↳ 云端 OCR 失败（{type(e).__name__}: {e}），改用本地")
            if local_ok:
                return self._run_local_ocr(t, md_path, allow_cloud=False)

        # 先尝试本地
        if local_ok:
            try:
                return self._run_local_ocr(t, md_path, allow_cloud=(not sensitive))
            except local_ocr.LocalOcrUnavailable as e:
                self.log(f"      ↳ 本地 OCR 不可用（{e}），改用云端")
            except Exception as e:
                self.log(f"      ↳ 本地 OCR 失败（{type(e).__name__}: {e}），改用云端")
            if sensitive:
                # 敏感目录本地失败也不能上云
                self.store.mark_skipped(src, t.size, t.mtime, t.route,
                                        f"敏感目录本地 OCR 失败：{type(e).__name__}")
                return "blocked", "敏感目录本地 OCR 失败（未上传）"

        # 云端兜底
        if self.ocr is None:
            self.store.mark_skipped(src, t.size, t.mtime, t.route, "OCR 未启用")
            return "skip", "OCR 未启用"
        return self._run_ocr(t, md_path)

    def _run_local_ocr(self, t: Task, md_path: Path, allow_cloud: bool = True) -> tuple[str, str]:
        """本地 RapidOCR 全流程：渲染 PDF → 逐页识别 → 启发式表格还原。"""
        src = t.src
        cfg = self.cfg.local_ocr

        if t.route == "img_ocr":
            # 单图片直接识别
            width = height = 0
            pages = [local_ocr.LocalOcrPage(0, 0, 0,
                     self.local_ocr.recognize_image(src).items)]
        else:
            # PDF 渲染成图再逐页识别
            rendered = local_ocr.render_pdf_pages(src, dpi=cfg.dpi)
            pages = []
            for img_path, w, h in rendered:
                page = self.local_ocr.recognize_image(img_path, w, h)
                page.index = len(pages)
                pages.append(page)
            try:
                shutil.rmtree(rendered[0][0].parent, ignore_errors=True) if rendered else None
            except Exception:
                pass

        # 逐页做启发式还原，检测复杂表
        parts: list[str] = []
        any_complex = False
        for page in pages:
            md, is_complex = converters.ocr_items_to_markdown(
                page.items,
                page_height=page.height,
                complex_col_threshold=cfg.complex_col_threshold,
            )
            if is_complex:
                any_complex = True
            parts.append(md)

        # 复杂表且允许云端 → 转发云端重做（VLM 保真）
        if any_complex and allow_cloud and cfg.ocr_complex_fallback and self.ocr is not None:
            self.log(f"      ↳ 检测到复杂表格，转发云端 PaddleOCR-VL-1.6 保真")
            return self._run_ocr(t, md_path)

        md = "\n\n".join(p for p in parts if p.strip())
        if not md.strip():
            raise RuntimeError("本地 OCR 结果为空")
        md = converters.cleanup_markdown(md)
        self._write_md(md_path, md, src.stem)
        self.store.mark_ok(src, t.size, t.mtime, "local-ocr", str(md_path), t.pages)
        with self.report.lock:
            self.report.ocr_files += 1
            self.report.ocr_pages += (t.pages or len(pages))
        return "ok", f"local-ocr({len(pages)}页)"

    def _run_ocr(self, t: Task, md_path: Path) -> tuple[str, str]:
        src = t.src
        if self.ocr is None:
            self.store.mark_skipped(src, t.size, t.mtime, t.route, "OCR 未启用")
            return "skip", "OCR 未启用"
        if is_sensitive(self.cfg, src):
            self.store.mark_skipped(src, t.size, t.mtime, t.route, "敏感目录，禁止上传云端")
            return "blocked", "敏感目录已拦截（未上传）"
        if self._ocr_paused:
            # 所有云端后端都已熔断，别一个个白耗，留到下次重跑
            self.store.mark_deferred(src, t.size, t.mtime, t.route, "云端后端全部不可用，本轮跳过")
            return "defer", "云端后端全部不可用，本轮暂停 OCR"

        pages = t.pages or 1
        try:
            with self._ocr_sem:
                res = self.ocr.convert_file(src, md_path, pages=pages)
        except QuotaExceeded as e:
            # 当日配额用尽：明天重跑即可，不算失败
            self.store.mark_deferred(src, t.size, t.mtime, t.route, str(e))
            return "defer", "云端 OCR 配额已用尽"
        except BackpressureExhausted as e:
            self.store.mark_deferred(src, t.size, t.mtime, t.route, str(e))
            self._note_backend_down(e)
            return "defer", "云端后端拥塞，稍后重试"
        except NetworkUnavailable as e:
            # 连不通 / 超时 / 5xx：可能是地址配错、本机断网、服务已下线。
            # 与"文件有问题"无关，标记 deferred 留待重跑。
            self.store.mark_deferred(src, t.size, t.mtime, t.route, str(e))
            self._note_backend_down(e)
            return "defer", "云端后端无法连接，稍后重试"
        except BackendUnavailable as e:
            # 兜底：其余"后端整体不可用"（服务端临时故障等）
            self.store.mark_deferred(src, t.size, t.mtime, t.route, str(e))
            self._note_backend_down(e)
            return "defer", "云端后端不可用，稍后重试"
        md = converters.html_tables_to_markdown(res.markdown)
        md = converters.cleanup_markdown(md)
        self._write_md(md_path, md, src.stem)
        self.store.mark_ok(src, t.size, t.mtime, "ocr", str(md_path), pages)
        with self._down_lock:
            self._down_streak = 0
        with self.report.lock:
            self.report.ocr_files += 1
            self.report.ocr_pages += pages
            if res.backend:
                self.report.ocr_by_backend[res.backend] += 1
            if len(res.attempts) > 1:
                self.report.ocr_switched += 1
        self._note_no_image_backend(res.backend)
        extra = ""
        if res.attempts and len(res.attempts) > 1:
            extra = f"，经 {'→'.join(res.attempts)} 切换"
        if res.chunks > 1:
            extra += f"，分 {res.chunks} 段"
        if res.image_placeholders:
            # 后端只回了文字，插图丢了 —— 明确提示，别让它悄悄过去
            self.log(f"      ↳ [插图缺失] {src.name} 有 {res.image_placeholders} 处插图"
                     f"未被后端返回，md 中留有空占位（如需插图可换主后端重做）")
            with self.report.lock:
                self.report.ocr_missing_images += 1
        return "ok", f"ocr/{res.backend}({res.elapsed:.0f}s, {res.image_count}图{extra})"

    def _note_backend_down(self, err: Exception | None = None) -> None:
        """累计连续"云端后端不可用"次数；全部后端都不可用时熔断本轮剩余的 OCR。

        与单后端时代的区别：那时一个后端返回队列满就暂停整轮；现在先由路由器
        切换到备用后端，只有当**链路上所有后端都不能用**时才真正暂停。
        """
        with self._down_lock:
            self._down_streak += 1
            streak = self._down_streak
            threshold = max(1, self.cfg.ocr.backpressure_abort_streak)
            all_down = bool(self.ocr is not None and self.ocr.all_unavailable())
            if (streak >= threshold or all_down) and not self._ocr_paused:
                self._ocr_paused = True
                self.report.ocr_paused = True
                chain = "、".join(self.ocr.names) if self.ocr is not None else "-"
                self.log(
                    f"\n[熔断] 云端 OCR 链路全部不可用（后端：{chain}）"
                    + (f"；连续 {streak} 个文件遇到后端故障" if streak >= threshold else "")
                    + f"。\n"
                    f"       已暂停本轮剩余 OCR 任务（不影响本地转换），"
                    f"这些文件已标记为待重试。\n"
                    f"       原因：{str(err)[:160] if err else '后端不可用'}\n"
                    f"       可执行 python -m doc2md status 查看各后端熔断状态；\n"
                    f"       稍后执行 python -m doc2md retry 续跑。\n"
                )

    def _note_no_image_backend(self, backend: str) -> None:
        """某个"不返回插图"的后端第一次被用到时提示一次。

        为什么必须提示：纯文字后端产出的 md 里插图位置是**空的**，
        事后翻 md 完全看不出"这份文件本来就没有图"还是"后端没给图"。
        只提示一次，避免刷屏。
        """
        if not backend or self.ocr is None:
            return
        if backend in self._no_image_notified:
            return
        if self.ocr.returns_images(backend):
            return
        with self._down_lock:
            if backend in self._no_image_notified:
                return
            self._no_image_notified.add(backend)
        self.log(
            f"[提示] 后端 {backend} 只能识别文字、不返回插图。"
            f"本次由它处理的文件，正文里的图片位置会缺失"
            f"（要插图请把 paddle / mineru precision 排在它前面）。"
        )

    # ---------------- 批量执行 ----------------
    def run(self, dry_run: bool = False, limit: int = 0,
            on_progress: Callable[[int, int], None] | None = None) -> Report:
        self.log("[1/3] 扫描文件…")
        tasks: list[Task] = []
        for p in self.iter_files():
            t = self.plan(p)
            if t is None:
                continue
            tasks.append(t)
            if limit and len(tasks) >= limit:
                break

        self.log(f"[2/3] 待处理 {len(tasks)} 个文件（已按真实格式分流）")
        routes = Counter(t.route for t in tasks)
        for r, c in routes.most_common():
            self.log(f"      {r:<12} {c:>6}")
        ocr_tasks = [t for t in tasks if t.route == "pdf_ocr"]
        if ocr_tasks:
            kinds = Counter()
            for t in ocr_tasks:
                why = t.note or ""
                if why.startswith("无可用文本层"):
                    kinds["无文本层（纯扫描）"] += 1
                elif why.startswith("扫描页"):
                    kinds["扫描页 + OCR 文本层"] += 1
                elif why.startswith("文本层乱码"):
                    kinds["文本层 CID 乱码"] += 1
                elif why.startswith("隐形"):
                    kinds["隐形 OCR 文本层"] += 1
                else:
                    kinds["其他"] += 1
            self.log("      其中需 OCR 的原因分布：")
            for k, c in kinds.most_common():
                self.log(f"        {k:<20} {c:>5}")
        if dry_run:
            sensitive = sum(1 for t in ocr_tasks if is_sensitive(self.cfg, t.src))
            self.log(f"\n[试运行] 不写入任何文件。其中需 OCR {len(ocr_tasks)} 个"
                     f"（约 {sum(t.pages for t in ocr_tasks)} 页），"
                     f"其中敏感目录已拦截 {sensitive} 个")
            return self.report

        self.log("[3/3] 开始转换…\n")
        local_pool, ocr_pool = [], []
        for t in tasks:
            (ocr_pool if t.route in ("pdf_ocr", "img_ocr") else local_pool).append(t)

        done = 0
        total = len(tasks)
        counter_lock = threading.Lock()

        def work(t: Task) -> None:
            nonlocal done
            status, info = self.process(t)
            with counter_lock:
                done += 1
                idx = done
            if status == "ok":
                self.report.ok += 1
                self.report.add_engine(info.split("(")[0])
            elif status == "skip":
                self.report.skipped += 1
            elif status == "blocked":
                self.report.blocked += 1
            elif status == "defer":
                self.report.deferred += 1
            else:
                self.report.failed += 1
                self.report.add_error(str(t.src), info)
            if self.verbose:
                mark = {"ok": "OK  ", "skip": "--  ", "fail": "FAIL",
                        "blocked": "BLK ", "defer": "HOLD"}[status]
                rel = _short(t.src)
                self.log(f"[{idx:>5}/{total}] {mark} {info:<22} {rel}")
            if on_progress:
                on_progress(idx, total)

        threads: list[threading.Thread] = []

        def spawn(pool_tasks, workers):
            q = list(pool_tasks)
            q_lock = threading.Lock()

            def runner():
                while True:
                    with q_lock:
                        if not q:
                            return
                        t = q.pop(0)
                    try:
                        work(t)
                    except Exception as e:
                        self.report.failed += 1
                        self.report.add_error(str(t.src), f"调度异常: {e}")

            for _ in range(max(1, workers)):
                th = threading.Thread(target=runner, daemon=True)
                th.start()
                threads.append(th)

        local_workers = int(self.cfg.raw.get("local_concurrency", 4))
        # OCR 线程数取各后端并发之和：不同后端可以并行，同一后端由它自己的信号量限流
        ocr_workers = max(1, self.ocr.pool_size if self.ocr is not None
                          else self.cfg.ocr.concurrency)
        spawn(local_pool, local_workers)
        spawn(ocr_pool, ocr_workers)
        for th in threads:
            th.join()

        self.com.shutdown()
        return self.report

    def close(self) -> None:
        try:
            self.com.shutdown()
        except Exception:
            pass
        if self.ocr is not None:
            try:
                self.ocr.close()
            except Exception:
                pass
        if self.local_ocr is not None:
            try:
                self.local_ocr.close()
            except Exception:
                pass


def _walk(root: Path):
    """稳定遍历，顺便剪掉排除目录。"""
    import os

    for dirpath, dirnames, filenames in os.walk(str(root)):
        yield dirpath, dirnames, filenames


def _drop_empty_dir(p: Path | None) -> None:
    """删掉空的资源目录。

    注意：文档没有内嵌图片时，这个目录压根不会被创建，
    所以必须先 is_dir() 再 iterdir()，否则会抛 FileNotFoundError。
    """
    if p is None:
        return
    try:
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    except OSError:
        pass


_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)>\s]+)")
_MD_IMG_RE = re.compile(r"<img[^>]*?\ssrc=[\"']([^\"']+)[\"']", re.I)


def _prune_assets(md_path: Path, text: str) -> int:
    """删掉 `.assets` 里新 md 已不再引用的文件（覆盖旧 md 时调用）。

    只动 `<md同名>.assets` 这个由本工具生成的目录 —— 按文件名精确比对，
    被链接引用的文件一律保留，不做递归删除以外的任何推断。
    """
    d = converters.assets_dir_for(md_path)
    try:
        if not d.is_dir():
            return 0
    except OSError:
        return 0

    keep = set()
    for rx in (_MD_LINK_RE, _MD_IMG_RE):
        for m in rx.finditer(text):
            raw = m.group(1).split("?", 1)[0].split("#", 1)[0]
            name = os.path.basename(unquote(raw.replace("\\", "/")))
            if name:
                keep.add(name.lower())

    removed = 0
    try:
        entries = sorted(d.rglob("*"), key=lambda q: len(q.parts), reverse=True)
    except OSError:
        return 0
    for p in entries:
        try:
            if p.is_file() and p.name.lower() not in keep:
                p.unlink()
                removed += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            continue
    return removed


def _short(p: Path, keep: int = 2) -> str:
    parts = p.parts
    if len(parts) <= keep + 1:
        return str(p)
    return str(Path(*parts[-(keep + 1):]))
