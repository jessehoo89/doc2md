"""COM 通道：把老式 OLE2 的 .doc/.xls/.wps/.et 转成 OOXML，再走本地解析。

WPS 与 MS Office 都注册了 COM，优先 WPS（对 .wps/.et 兼容更好）。
COM 对象非线程安全，因此每个工作线程各持一份实例。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

WD_FORMAT_DOCX = 16          # wdFormatDocumentDefault
XL_FORMAT_XLSX = 51          # xlOpenXMLWorkbook
ROUND_TRIP_MAX = 12          # 超过则重建 COM 实例


class ComUnavailable(RuntimeError):
    pass


class ComConverter:
    """线程安全的 WPS/Office COM 转换器。"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._local = threading.local()
        self._counts: dict[int, int] = {}
        self._lock = threading.Lock()
        self._app_name = ""

    # ---------- 实例管理 ----------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _ensure_init(self) -> None:
        import pythoncom

        if not getattr(self._local, "inited", False):
            pythoncom.CoInitialize()
            self._local.inited = True

    def _word_app(self):
        app = getattr(self._local, "word", None)
        if app is not None:
            return app
        import win32com.client as wc

        last_err = None
        for prog in ("Kwps.Application", "Word.Application"):
            try:
                app = wc.DispatchEx(prog)
                try:
                    app.Visible = False
                    app.DisplayAlerts = 0
                except Exception:
                    pass
                self._local.word = app
                self._local.word_name = prog
                self._log(f"[COM] 已启动 {prog}")
                return app
            except Exception as e:  # noqa: PERF203
                last_err = e
        raise ComUnavailable(f"无法创建 WPS/Word COM 实例: {last_err}")

    def _excel_app(self):
        app = getattr(self._local, "excel", None)
        if app is not None:
            return app
        import win32com.client as wc

        last_err = None
        for prog in ("Ket.Application", "Excel.Application"):
            try:
                app = wc.DispatchEx(prog)
                try:
                    app.Visible = False
                    app.DisplayAlerts = False
                except Exception:
                    pass
                self._local.excel = app
                self._local.excel_name = prog
                self._log(f"[COM] 已启动 {prog}")
                return app
            except Exception as e:  # noqa: PERF203
                last_err = e
        raise ComUnavailable(f"无法创建 WPS/Excel COM 实例: {last_err}")

    def _recycle(self, kind: str) -> None:
        """轮转次数过多时重建实例，避免 COM 长时间运行后卡死。"""
        key = threading.get_ident()
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            over = self._counts[key] >= ROUND_TRIP_MAX
            if over:
                self._counts[key] = 0
        if over:
            self._quit(kind)

    def _quit(self, kind: str) -> None:
        attr = "word" if kind == "word" else "excel"
        app = getattr(self._local, attr, None)
        if app is None:
            return
        try:
            app.Quit()
        except Exception:
            pass
        setattr(self._local, attr, None)
        self._log(f"[COM] 已释放 {kind} 实例")

    def shutdown(self) -> None:
        for kind in ("word", "excel"):
            self._quit(kind)
        if getattr(self._local, "inited", False):
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._local.inited = False

    # ---------- 转换 ----------
    def doc_to_docx(self, src: Path, dst: Path) -> Path:
        """老式 Word 文档 → .docx（写入 dst 并返回）。"""
        self._ensure_init()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        app = self._word_app()
        doc = None
        try:
            doc = app.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
            doc.SaveAs2(str(dst), FileFormat=WD_FORMAT_DOCX)
            return dst
        finally:
            if doc is not None:
                try:
                    doc.Close(False)
                except Exception:
                    pass
            self._recycle("word")

    def xls_to_xlsx(self, src: Path, dst: Path) -> Path:
        """老式 Excel 工作簿 → .xlsx（写入 dst 并返回）。"""
        self._ensure_init()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        app = self._excel_app()
        wb = None
        try:
            wb = app.Workbooks.Open(str(src), ReadOnly=True, UpdateLinks=0)
            wb.SaveAs(str(dst), FileFormat=XL_FORMAT_XLSX)
            return dst
        finally:
            if wb is not None:
                try:
                    wb.Close(False)
                except Exception:
                    pass
            self._recycle("excel")

    def to_ooxml(self, src: Path, kind: str) -> Path:
        """按真实类型把老式文件转成 OOXML，返回临时文件路径（调用方负责删除）。"""
        tmpdir = Path(tempfile.mkdtemp(prefix="doc2md_com_"))
        stem = src.stem[:80] or "file"
        if kind in ("doc",):
            return self.doc_to_docx(src, tmpdir / f"{stem}.docx")
        if kind in ("xls",):
            return self.xls_to_xlsx(src, tmpdir / f"{stem}.xlsx")
        # 未知的 OLE2 容器：先试 Word，再试 Excel
        try:
            return self.doc_to_docx(src, tmpdir / f"{stem}.docx")
        except Exception:
            return self.xls_to_xlsx(src, tmpdir / f"{stem}.xlsx")


def cleanup_temp(path: Path) -> None:
    """删除 COM 转换产生的临时目录。"""
    try:
        shutil.rmtree(str(path.parent), ignore_errors=True)
    except Exception:
        pass


def com_available() -> tuple[bool, str]:
    """探测 COM 是否可用，返回 (可用, 说明)。"""
    try:
        import win32com.client as wc

        for prog in ("Kwps.Application", "Word.Application"):
            try:
                app = wc.DispatchEx(prog)
                try:
                    app.Visible = False
                except Exception:
                    pass
                try:
                    app.Quit()
                except Exception:
                    pass
                return True, prog
            except Exception:
                continue
        return False, "未找到可用的 WPS/Word COM"
    except ImportError:
        return False, "未安装 pywin32"


def wait_process_quiet(seconds: float = 0.0) -> None:
    if seconds > 0:
        time.sleep(seconds)
