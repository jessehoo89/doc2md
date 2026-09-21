"""doc2md —— 文档批量转 Markdown 工具。

把指定目录下的 docx/doc/xls/xlsx/pdf 等批量转成 Markdown，
保存在原目录同名 .md；无文字层的扫描件自动走 PaddleOCR 云端识别；
并支持实时监控目录，新增或修改的文件自动转换。
"""
from .config import Config, load_config
from .state import StateStore

__version__ = "1.0.0"
__all__ = ["Config", "load_config", "StateStore", "__version__"]
