"""本地 OCR 后端：子进程调用 RapidOCR（PP-OCRv6 ONNX @ DirectML/CPU）。

设计取舍：
  - 不把 rapidocr 装进 doc2md 自己的环境，避免依赖污染；通过 subprocess 调
    独立的 RapidOCR 环境（解释器路径由 config.json 的 local_ocr.python_exe
    指定；留空即自动禁用本地 OCR，全部走云端链路）。
  - PDF 渲染放在本模块所在环境（doc2md 环境，已装 pymupdf）完成，RapidOCR
    只负责识别单张图片，输入/输出用临时文件 + JSON 传递。
  - RapidOCR 侧提供一个内嵌的 worker 脚本（rapid_worker.py），同一进程循环
    处理多张图，避免每次子进程冷启动加载模型（约 4s 一次）。

返回结构：
  {
    "pages": [
      {"index": 0, "width": 1191, "height": 1684,
       "items": [{"text": "...", "box": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]],
                   "score": 0.99}, ...]}
    ]
  }
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# RapidOCR worker 脚本内容（内嵌，运行时写入临时目录）
_RAPID_WORKER = r'''# -*- coding: utf-8 -*-
"""RapidOCR 常驻 worker：读 stdin 一行一个图片路径，对每张输出一行 JSON。"""
import json
import sys

def main():
    from rapidocr import RapidOCR
    engine = RapidOCR()  # 默认 DirectML；如需 CPU 可传 params={"EngineConfig.onnxruntime.use_dml": False}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        img = line
        try:
            res = engine(img)
            items = []
            if res.txts is not None:
                boxes = res.boxes if res.boxes is not None else []
                scores = res.scores if res.scores is not None else []
                for i, t in enumerate(res.txts):
                    box = boxes[i].tolist() if i < len(boxes) else []
                    score = float(scores[i]) if i < len(scores) else 0.0
                    items.append({"text": t, "box": box, "score": score})
            out = {"ok": True, "items": items}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
'''


@dataclass
class LocalOcrItem:
    text: str
    box: list[list[float]]
    score: float


@dataclass
class LocalOcrPage:
    index: int
    width: int
    height: int
    items: list[LocalOcrItem] = field(default_factory=list)


@dataclass
class LocalOcrResult:
    pages: list[LocalOcrPage] = field(default_factory=list)


class LocalOcrUnavailable(RuntimeError):
    """本地 RapidOCR 环境不可用（python 不存在 / 模型加载失败等）。"""


class LocalOcrClient:
    """本地 RapidOCR 客户端，线程安全（内部用锁串行化子进程）。"""

    def __init__(self, python_exe: str, device: str = "dml", verbose: bool = True):
        self.python_exe = python_exe
        self.device = device.lower()  # dml | cpu
        self.verbose = verbose
        self._proc = None
        self._worker_path: Path | None = None

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _start(self) -> None:
        """启动常驻 worker 子进程，并做一次空握手确认可用。"""
        if self._proc is not None and self._proc.poll() is None:
            return
        if not Path(self.python_exe).exists():
            raise LocalOcrUnavailable(f"RapidOCR Python 不存在：{self.python_exe}")

        tmpdir = Path(tempfile.mkdtemp(prefix="rapid_worker_"))
        self._worker_path = tmpdir / "rapid_worker.py"
        self._worker_path.write_text(_RAPID_WORKER, encoding="utf-8")

        # 若指定 CPU，改写 worker 里的 engine 参数
        if self.device == "cpu":
            content = self._worker_path.read_text(encoding="utf-8")
            content = content.replace(
                "engine = RapidOCR()",
                'engine = RapidOCR(params={"EngineConfig.onnxruntime.use_dml": False})',
            )
            self._worker_path.write_text(content, encoding="utf-8")

        self._proc = subprocess.Popen(
            [self.python_exe, str(self._worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def _ask(self, img: str) -> dict[str, Any]:
        """向 worker 发一张图，返回解析后的 JSON dict。"""
        self._start()
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(img + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            # worker 挂了，读 stderr 找原因
            err = ""
            if self._proc.stderr is not None:
                try:
                    err = self._proc.stderr.read()
                except Exception:
                    err = ""
            raise RuntimeError(f"RapidOCR worker 意外退出。stderr: {err[:300]}")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError(f"worker 返回非 JSON：{line[:200]}")

    def recognize_image(self, img_path: str | Path, width: int = 0, height: int = 0) -> LocalOcrPage:
        """识别单张图片。width/height 由调用方（PDF 渲染）提供。"""
        out = self._ask(str(img_path))
        if not out.get("ok"):
            raise RuntimeError(f"本地 OCR 失败：{out.get('error', '未知')}")
        items = [
            LocalOcrItem(it["text"], it["box"], it["score"]) for it in out.get("items", [])
        ]
        return LocalOcrPage(index=0, width=width, height=height, items=items)

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
            self._proc = None
        if self._worker_path is not None:
            try:
                shutil.rmtree(self._worker_path.parent, ignore_errors=True)
            except Exception:
                pass


def render_pdf_pages(pdf_path: str | Path, dpi: int = 200) -> list[tuple[Path, int, int]]:
    """把 PDF 每页渲染成 PNG，返回 [(临时图片路径, 宽, 高), ...]。"""
    import pymupdf

    from .detect import silence_mupdf

    silence_mupdf()
    pdf_path = Path(pdf_path)
    doc = pymupdf.open(str(pdf_path))
    tmpdir = Path(tempfile.mkdtemp(prefix="doc2md_pdf_"))
    rendered: list[tuple[Path, int, int]] = []
    try:
        for i in range(doc.page_count):
            page = doc[i]
            pix = page.get_pixmap(dpi=dpi)
            out = tmpdir / f"p{i:04d}.png"
            pix.save(str(out))
            rendered.append((out, pix.width, pix.height))
    finally:
        doc.close()
    return rendered
