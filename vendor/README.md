# vendor/ —— 第三方子项目（原样引入，可回灌上游）

本目录放**从上游仓库引入的第三方代码**。与「把别人的实现抄一遍」不同，这里保留
完整的上游文件（含 README / LICENSE / 测试 / CI 配置），目的是：

* 上游更新了能同步过来；
* 我们对上游的修正能作为 patch 提回去（PR）；
* 不会出现「两份实现各自漂移、互相打脸」。

---

## ZhDocParser

| 项 | 值 |
|---|---|
| 上游 | https://github.com/melonelish/ZhDocParser |
| 版本 | v0.3.0 |
| 引入时的 commit | `7e1845f3d93d54605b28556152a94bc13dc7f45d` |
| 引入日期 | 2026-10-03 |
| 许可 | MIT（见 `ZhDocParser/LICENSE`） |
| 引入方式 | `git subtree add --squash` |

**用途**：doc2md 的 `pdf_engine="rule"` 档（纯规则、零神经网络的 PDF → Markdown）。
接入层在 `doc2md/pdf_zhdoc.py`，**只调它的 `PdfExtractor`**，不碰 web / CLI 层。

### 引入命令（备忘）

```bash
git subtree add --prefix=vendor/ZhDocParser \
    https://github.com/melonelish/ZhDocParser main --squash
```

### 上游更新怎么拉

```bash
git subtree pull --prefix=vendor/ZhDocParser \
    https://github.com/melonelish/ZhDocParser main --squash
```

我们在下面 4 处动了上游文件，若与上游新提交撞车会冲突，按注释里的理由手动合并即可
（多数情况下上游改的是别处，冲突概率不高）。

### 我们对上游做的修改（都可单独 PR 回去）

| # | 文件 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | `zhdocparser/__init__.py` | 顶层 `from zhdocparser.sdk import …` → PEP 562 `__getattr__` 惰性导入 | 顶层导入会让**任何**子模块导入（哪怕只要 `extractors.pdf_extractor`）都连带载入 sdk → service → extractors.factory → docx_extractor → **python-docx / lxml**。只做 PDF 的调用方白白背上这套依赖 |
| 2 | `extractors/pdf_extractor.py` | `import fitz` → `import pymupdf as fitz` | PyMuPDF 1.28 起 `fitz` 会往 stderr 打 `The 'fitz' API is deprecated…`，且官方声明未来会移除 |
| 3 | `extractors/pdf_extractor.py` | `_heading_pattern_level` 的正则**顺序**：`^\d+\.\d+\.\d+` 提到 `^\d+[、.]` 之前，并给后者加 `(?!\d)` | 原顺序里 `^\d+[、.]` 在前，`1.1.1 xxx` 会被它的前缀 `1.` 抢先匹配，**三级标题被降级成二级**；加 `(?!\d)` 则避免把 `1.5 倍…` 这类正文误判成编号标题 |
| 4 | `extractors/pdf_extractor.py` | `_extract_page_lines` 把行 bbox 用 `page.rotation_matrix` 归一到**显示坐标系** | `get_text()` 返回的是未旋转坐标，而 `page.rect` 是旋转后的显示尺寸。`rotation != 0` 的 PDF（WPS 导出的红头文件就是）两者不同尺度，会让「整行宽度 ≥ 页宽 × 0.65」这类判断全线错位，把单栏正文误判成多栏、**重排出完全错误的阅读顺序（正文被排到标题前面）**。实测本机语料里一份公文：修复前 `title` 抓到正文行，修复后正确 |

### 怎么把这些改动 PR 回上游

```bash
# 1) 从本仓库导出针对 vendor 目录的补丁
#    12d3c22 = 引入 vendor 的提交（其内容与上游 7e1845f 完全一致，未做任何改动）
git diff 12d3c22 -- vendor/ZhDocParser > zhdocparser-fixes.patch

# 2) fork 上游，在干净分支上应用
git clone https://github.com/<你的账号>/ZhDocParser && cd ZhDocParser
git checkout -b fix/rotation-and-numbering
git apply ../zhdocparser-fixes.patch
git commit -m "fix: 旋转页面坐标归一 / 编号正则顺序 / 惰性导入 / 去掉 fitz 别名"
git push -u origin fix/rotation-and-numbering

# 3) 开 PR
```

### 打包注意（`doc2md.spec`）

`vendor/ZhDocParser` **不在默认搜索路径上**，且接入层是在**函数体内** import 的，
两件事都必须在 spec 里显式声明，否则源码态一切正常、打出来的 exe 一跑 rule 档就
`ImportError`：

* `pathex` 追加 `vendor/ZhDocParser`；
* `hiddenimports` 里列出 `zhdocparser.extractors.pdf_extractor` 等要用的模块。

**只列 PDF 链路需要的模块**：不列 `zhdocparser.api` / `app` / `cli`，它们的
fastapi / uvicorn / typer 就不会进包；不列 `extractors.factory`，python-docx / lxml
也不会进包。唯一必须新增的运行时依赖是 **pydantic**（`zhdocparser.schemas` 用它）。

### 已知的能力边界（沿用上游声明）

上游 README 的 Current Scope 写明**不含**：OCR、扫描件图像理解、PPT/Excel、
复杂合并单元格重建。所以它只接 doc2md 的 `pdf_text` 这一条路 —— 扫描件仍走
云端 OCR、Office 仍走本地/COM，各归各的。
