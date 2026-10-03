# doc2md 使用说明

[README](../README.md) 只保留项目介绍、安装方法、支持平台和常用命令；这里是完整版：
安装细节、每条命令的参数、图形界面、配置字段、云端 OCR 链路、目录结构、打包与安装包、
开发测试、边界与注意事项。

## 安装细节

### Windows：源码安装

```bat
:: 1) 建虚拟环境（一次即可）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2) 生成配置文件，然后把 roots 改成你的语料目录
copy config.example.json config.json

:: 3) 填云端 OCR 凭据（不填也能跑，只是扫描件没有云端后端）
copy .env.example .env

:: 4) 试运行：看清每个文件会走哪条通道，不写任何文件
.venv\Scripts\python.exe -m doc2md scan

:: 5) 正式转换 / 常驻监控
.venv\Scripts\python.exe -m doc2md run
.venv\Scripts\python.exe -m doc2md watch
```

不想敲命令就**双击 `文档转MD.bat`**，它带一个中文菜单（扫描 / 转换 / 监控 /
统计 / 重试 / 检测云端 OCR / 打开配置 / 打开凭据）。`云端OCR自检.bat` 专门做
后端连通性 + 凭据生效情况自检。

**想要窗口界面就双击 `文档转MD-GUI.bat`**，或者在上面的菜单里按 `G`。图形界面
与命令行共用同一套核心，功能完全一样 —— 详见 [图形界面（GUI）](#图形界面gui)。

`_python.bat` 是给上面几个 bat 共用的解释器探测脚本，**不要单独双击**。
查找顺序：`%DOC2MD_PYTHON%` → `.venv\Scripts\python.exe` → `venv\Scripts\python.exe`
→ PATH 上第一个真能跑的 `python`（微软商店那个占位程序会被识别并跳过）。

### Linux / macOS：一键脚本或源码安装

一键脚本（推荐）：

```bash
sudo apt install -y libreoffice-writer libreoffice-calc   # 老式 .doc/.xls 才需要

bash install.sh                    # 装到 ~/.local，装完就有 doc2md 命令
bash install.sh --prefix /opt/doc2md
bash install.sh --source           # 强制源码方式（建 venv + 装依赖）
bash install.sh --venv 目录        # 源码方式的虚拟环境放哪 / 复用哪个
bash install.sh --bin 文件         # 指定现成的单文件可执行程序
bash install.sh --mirror URL       # 直连 PyPI 失败时用的镜像（默认清华源）
bash install.sh --uninstall        # 卸载（会先列出将删除的内容并确认）
```

- **两种装法自动挑**：脚本先找单文件可执行程序（`--bin` 指定 → `$DOC2MD_BIN` →
  仓库里 `dist-onefile/doc2md` → 脚本旁边叫 `doc2md` 的可执行文件）；找不到才退回
  源码方式建 venv、装 `requirements.txt`。前者不需要 Python，后者需要 3.11+。
- 程序会把 `config.json` / `.env` / `state.db` 写在**安装目录的 `share/doc2md/`**
  （源码方式则是仓库根），跟着程序走；`install.sh` 顺手把 `.env.example` 铺成
  `.env`（权限 0600，值留空）。
- 装完会自检 LibreOffice、`$PREFIX/bin` 是否在 `PATH`，并跑一次 `doc2md --version`
  验证。没在 `PATH` 里就照它给的 `export PATH=...` 加一行（可写进 `~/.bashrc`）。
- 卸载只删启动器与 `share/doc2md/`（**含 state.db，删了要全量重转**），不动仓库
  和虚拟环境。

源码安装：

```bash
# 1) 老式 .doc/.xls 的转换靠 LibreOffice（Debian/Ubuntu，只要 Writer + Calc）
sudo apt install -y libreoffice-writer libreoffice-calc

# 2) 建虚拟环境（一次即可）
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 3) 生成配置文件，然后把 roots 改成你的语料目录
cp config.example.json config.json

# 4) 填云端 OCR 凭据（不填也能跑，只是扫描件没有云端后端）
cp .env.example .env

# 5) 试运行：看清每个文件会走哪条通道，不写任何文件
.venv/bin/python -m doc2md scan

# 6) 正式转换 / 常驻监控
.venv/bin/python -m doc2md run
.venv/bin/python -m doc2md watch
```

Linux 下没有 `.bat` 入口：`scan / convert / run / watch / stats / retry` 这些菜单项
都有同名子命令，`python -m doc2md --help` 可查。图形界面（`gui.py`，Tkinter）在
Linux 上未验证，需要自备带 tkinter 的完整 Python。

### 装完先做三件事

1. **改 `config.json` 的 `roots`** —— 空列表等于什么都不转；文件首次运行自动生成。
2. **填 `.env`**（可选）—— 云端 OCR 凭据；不填时扫描件没有云端后端，其余照常。
3. **`doc2md scan` 试运行** —— 只看会走哪条通道，不写任何文件。

## 命令行

> 下面示例里写的是 Windows 的 `doc2md.exe`；Linux / macOS 上换成 `doc2md`
> （源码运行时是 `python -m doc2md`），参数完全一致。

| 命令 | 作用 |
|---|---|
| `python -m doc2md scan` | 试运行：列出待转文件与各自通道，**不写任何文件** |
| `python -m doc2md run` | 批量转换，中断后重跑自动续传 |
| `python -m doc2md watch` | 常驻监控新增/修改的文件并自动转换 |
| `python -m doc2md test <文件>` | 只转一个文件（想看分流详情用这个） |
| `python -m doc2md convert <路径…>` | **只转点名的文件**，不扫整目录（见 [只转一批文件](#只转一批文件convert)） |
| `python -m doc2md status` | 统计（含各后端今日用量） |
| `python -m doc2md retry` | 重试失败的文件（`--clear` 仅清记录） |
| `python -m doc2md ping` | 检测各云端 OCR 后端的就绪与连通性 |
| `python -m doc2md env` | 查看 `.env` 与各后端 Token 的生效情况 |

公共参数：`--config <路径>`、`--root <目录>`（可重复，覆盖配置）、
`--limit N`、`--quiet`、`--no-ocr`。写在子命令前后都认。

`run` 还多一个 `--redo-engine <引擎>`：

```bat
:: 换了 PDF 引擎（pdf_engine: layout → rule）后，把文字层 PDF 全部重转一遍
doc2md.exe run --redo-engine pdf-text
```

原因：状态库里的"已成功"记录会让这些文件**被跳过**（跳过判定 = 库里有记录 **且**
md 存在）。这个参数只清记录、**不删已产出的 md**，重转时覆盖它们。
可选值就是状态库里的引擎名：`pdf-text` / `docx` / `xlsx` / `html` / `com-docx` /
`com-xlsx`（填错会提示当前库里实际有哪些）。想看分布：

```bat
doc2md.exe status
```

### 只转一批文件：`convert`

`run` 是"扫 `roots` 下的全部文件"，但实际经常是"**就转这一批**"（补转某几百份、
或者别处拷来的一批）。为此每次去改 `config.json` 的 `roots` 太别扭，`convert` 就是干这个的：

```bat
:: 1) 直接在命令行列文件（也可以给目录，按扩展名递归展开）
doc2md.exe convert a.docx b.pdf "E:\语料\某一批"

:: 2) 给一个清单文件：每行一个路径，# 开头是注释，空行忽略
doc2md.exe convert --list 清单.txt

:: 3) 从管道读（省得先落一个临时文件）
dir /b /s *.pdf | doc2md.exe convert --list -

:: 先看解析对不对、各走哪条通道（不写文件，也不清记录）
doc2md.exe convert --list 清单.txt --dry-run

:: 强制重转（不加则已转过的按幂等跳过）
doc2md.exe convert --list 清单.txt --force
```

双击版菜单里也有：**`[L] 按清单批量转换`** —— 会先 `--dry-run` 给你过目，再问一句才真转。

清单的容错（都是实际会遇到的情况）：

- **编码**：`dir /b > 清单.txt` 出来的是 GBK、记事本另存常带 UTF-8 BOM、别处拷来的
  可能是 UTF-16。按 BOM 判断，无 BOM 时先试 UTF-8 再退 GBK —— **不用自己转码**。
- 行首尾空白、`"带引号的路径"`（资源管理器「复制为路径」给的就是这种）、
  从 Excel 粘来的 `路径<Tab>备注` 都会被正确清洗。
- 相对路径先相对当前目录找，找不到再**相对清单文件所在目录**找 ——
  清单和文件放一起是最自然的用法。
- 目录行会按 `watch_extensions` 递归展开，过滤规则与整目录扫描**完全一致**
  （共用 `engine.iter_docs`，不会出现"整目录跑会转、清单里写同一个目录却不转"）。

⚠ **找不到的条目会被明确报出来**。清单里写错一个字符不会抛异常、只会安静地少转
一份文件，所以这里宁可多打几行：

```
  解析结果 : 3 个文件，去重 1，格式不支持 1，找不到 1

  [找不到] 1 条（清单里写了，磁盘上没有）：
    - 不存在的文件.docx
```

转换本身走 `run` 的同一套流程 —— 幂等跳过、敏感目录拦截、OCR 故障熔断、断点续传
一律照旧。清单里的文件**不要求在 `roots` 之下**；但那种情况下 `output.layout = mirror`
算不出相对路径，会**按文件名平铺到输出根**（CLI 会明确提示，想保留目录结构就用
`--root` 指定它们的上级目录）。

## 图形界面（GUI）


![图形界面](gui-screenshot.png)

左边只有三组**要经常调**的配置（处理目录 / 输出方式 / 各项开关，小窗口或高缩放下可滚动），
右边是实时日志（按错误、警告、成功着色），底部是进度条。

首页刻意不放低频动作：**保存**（每个动作执行前都会先写回 `config.json`，不用手动存）、
**打开配置文件 / 凭据文件 / 程序目录** 都收在菜单 `文件` 里。启动时会把当前生效的
引擎、后端链路、凭据文件、状态库打到日志面板开头，需要对照时看日志就行。

四种打开方式，随便挑一个：

```bat
:: 1) 双击（推荐）
文档转MD-GUI.bat
:: 2) 菜单版里按 G
文档转MD.bat  →  G
:: 3) 直接用解释器跑
.venv-gui\Scripts\python.exe gui.py
:: 4) 打包后的窗口版 exe
dist\doc2md\doc2md-gui.exe
```

界面上能做的事和命令行**一一对应**，因为它就是把这些调用搬进了窗口，
没有第二套实现：

| 界面上的按钮 | 等价命令 |
|---|---|
| 扫描试运行 | `scan` |
| 开始转换 | `run` |
| 启动监控 | `watch` |
| 转换单个文件… | `test <文件>` |
| 查看统计 | `status` |
| 重试失败 | `retry` |
| 检测云端 OCR | `ping` + 凭据自检 |
| 设置… | 改 `config.json` 与 `.env`（含云端 OCR Token），等价于手工编辑这两个文件 |
| 重新加载配置 | 手工改完 `config.json` 后重新读取（并同步左侧面板），不用重启程序 |

左侧的**处理目录 / 输出方式 / 保留原文件 / 云端 OCR / 本地 OCR** 改动会直接
写回 `config.json`（原子替换，`_xxx说明` 注释与多后端链路配置原样保留），
所以界面上看到的就一定是你实际在用的配置，不会出现"界面改了但程序没改"。

**日志面板与命令行输出完全一致**，可直接对照排查；`运行日志 → 另存为` 能把
整段日志存成 txt。

### 设置窗口（`文件 → 设置…` / 工具栏「设置…」/ `Ctrl+,`）

常规配置与凭据填写都收在这一个窗口里，四个页签：

| 页签 | 管什么 |
|---|---|
| 转换与引擎 | **`pdf_engine` 引擎选档**、保留原文件、文本层可信度检查、敏感目录拦云端、Excel 读取上限、并发与防抖 |
| 目录与输出 | 处理目录（增删 / 排序）、输出位置与目录结构、同名冲突策略、状态库与日志路径 |
| 词表与扩展名 | 排除目录名、敏感词、监控扩展名、图片扩展名（一行一条） |
| 云端 OCR Token | `CRED_FIELDS` 的全部凭据键，写 `.env` |

**文本型 PDF 的引擎选档**：默认 `rule`（纯规则提取；中文公文 / 国标上标题识别更准，
约快 15 倍）。要换回旧的模型档就在这个页签选 `layout`。改完只影响**之后**转的文件，
已转出的 md 不会自动重做 —— 想让它们按新引擎重来，命令行跑 `run --redo-engine pdf-text`。

几条使用上的约定：

- 保存后**左侧配置面板立刻同步**（设置里改过的处理目录 / 输出目录马上反映出来）；
- 常规配置写 `config.json` 是**原子替换 + 保留其它键**，`_xxx说明` 注释与多后端链路配置原样留下；
- 凭据**只写你真正改过的键**：与系统环境变量同值的不写回文件，免得把环境变量里的 Token 顺手抄进本机；
- **保存立即生效，不用重启**：写盘的同时就把值灌进当前进程的环境变量；想顺手验一下连通性就点「保存并检测连通性」；
- 写入是**原子替换**，并且**保留你手写的注释、空行、顺序和工具不认识的键** ——
  不会把你整理过的 `.env` 冲成模板；
- 值里含 `#` 或首尾空格时会自动加引号，避免读回来被当成行尾注释截断；
- **内容区可滚动，而「保存」按钮固定在窗口底部、不在滚动区里**。这是刻意设计的：
  早先那个独立的「填写 Token」小窗口把按钮和内容放在同一个网格里，还禁止缩放，
  一展开高级选项按钮就被顶出屏幕，而且无处可滚 —— 用户根本点不到保存。
  现在高级项**默认就展开**（整页能滚，不需要再折叠），这个坑从根上没有了。

**第一次打开程序**（还没填过任何 Token）时会把设置窗口直接开到「云端 OCR Token」页；
不填也能用 —— docx / xlsx / 有文字层的 PDF 照常转。点「稍后再说」会把记号写进
`.env` 的注释里，之后不再打扰，需要时从菜单或工具栏随时打开。

不想用界面也行：菜单 `文件 → 打开配置文件` / `打开凭据文件 (.env)` 用记事本改，效果一样。
两种方式写的是**同一个文件**（`config.json` 同目录的 `.env` 优先，其次是
`DOC2MD_ENV_FILE` 指定的、再其次是程序目录的），读写两侧用的是同一条查找顺序。

### 关于 `.venv-gui`：为什么需要第二个环境

图形界面用 **Tkinter**（Python 标准库，零新增依赖），但 **tkinter 不是纯 Python**——
它要 `_tkinter.pyd` + `tcl86t.dll` / `tk86t.dll` + tcl 脚本目录。

本机 WorkBuddy 托管的 Python 3.13 是**精简版**，这些文件一个都没有：

```
Lib/tkinter/__init__.py   →  不存在
DLLs/_tkinter.pyd         →  不存在
```

所以 `.venv` 里跑 GUI 会直接 `ModuleNotFoundError: No module named 'tkinter'`。
`.venv-gui` 是从本机另一套**完整 CPython**（自带 tkinter、Tk 8.6）建的，
专供图形界面与打包使用 —— **3.11 及以上任意完整版本都行**：

```bat
:: 一次即可（约几分钟，取决于网速）
python -m venv .venv-gui
.venv-gui\Scripts\python.exe -m pip install -r requirements.txt
```

> 建 `.venv-gui` 的那个 `python` 必须是**完整** CPython（官网安装版或 uv 装的
> 独立版本都可以）。判断标准只有一条：`python -c "import tkinter"` 不报错。
> uv 装的解释器用 `uv python list` 或看 `%APPDATA%\uv\python\` 下的目录名。
> 打包同理 —— 用哪个解释器建的 `.venv-gui`，就打进哪种运行时。

没建也不会出事：`文档转MD-GUI.bat` 会挑不到解释器并给出上面这两行命令，
菜单里的 `G` 也会明确提示"当前解释器没有 tkinter"。

### 「停止」按钮的确切语义

- **监控模式**：立即停止，`WatchService` 退出，监控句柄释放。
- **批量转换 / 重试**：**当前正在转换的那个文件会做完**，其余任务不再开始 ——
  不会留下半个 md。已完成的文件已经入库，所以再点一次「开始转换」会自动跳过它们，
  从中断处继续。这是刻意的：中途硬杀线程会让 Office COM（Linux 上是 soffice）进程和云端任务悬空。

## 配置要点


配置全在 `config.json`（字段都带 `_xxx说明` 注释）。最常改的几个：

| 字段 | 说明 |
|---|---|
| `roots` | **要转换的目录列表**，必须改，空列表等于什么都不转 |
| `output.mode` | `alongside` 与源文件同目录同名（默认）；`custom` 统一存到 `output.root` |
| `output.layout` | custom 模式下 `mirror` 保留原目录结构 / `flat` 全部平铺 |
| `output.on_collision` | 两个源文件抢同一个 md 名时怎么办，见下 |
| `overwrite_existing_md` | `overwrite`（默认）比源文件旧就重写 / `skip` 只在缺失时生成 |
| `sensitive_markers` | 命中这些目录名的文件**禁止上传云端** |
| `pdf_trust_check` | 复核文本层是否真的可信（防"扫描件自带 OCR 层 / CID 乱码 / 隐形层"） |
| `pdf_engine` | 文本型 PDF 走哪档引擎：`rule`（默认，纯规则，见下）/ `layout`（ONNX 版面模型，慢十几倍） |
| `local_ocr.python_exe` | 本地 RapidOCR 环境；**留空即自动禁用本地 OCR** |

### 文本型 PDF 走哪个引擎（`pdf_engine`）

| 值 | 走法 | 适合 |
|---|---|---|
| `rule`（**默认**） | **`vendor/ZhDocParser`** 的纯规则提取器（接入层 `doc2md/pdf_zhdoc.py`），零模型、**不 import `pymupdf4llm`** | 中文公文 / 国标 / 规程 / 汇编 |
| `layout`（可选） | `pymupdf4llm` + 49 MB ONNX 版面模型 | 复杂版面（杂志 / 海报）。项目早期语料都是它产出的 |

`rule` 档读的全是 PDF 里本来就有的数字：每行的**字号 / 粗体 / 坐标**，加上中文公文的
**编号**（`一、` / `（一）` / `1.1` / `第X条`），表格用 `page.find_tables()`（纯几何：
找线、找对齐）。同一份 10 页公文实测：

| | 转换 CPU | 标题数 | 认出的 `一、` / `（一）` |
|---|---|---|---|
| `layout` | 12.83 s | 2 | 全没认出来 |
| `rule` | **1.67 s** | **22** | 层级完整（3 个 `一、` + 12 个 `（一）`） |

**为什么规则反而更准**：那份公文的 `一、总体要求` **字号与正文完全相同**（16.0pt）。
页面上唯一能区分它和正文的东西，就是行首那三个字 `一、`。版面模型只看视觉特征
（字号 / 行距 / 位置），拿不到"编号 → 层级"这种文本语义；而中文公文和国标的层级是
**强约束**，正则一抓一个准。

**为什么 rule 档是 vendor 上游而不是自己写一份**：见 [`vendor/README.md`](../vendor/README.md)。
一句话 —— 上游更新能 `git subtree pull` 同步、我们对上游的修正能提 PR 回去、
不会分叉成两份实现各自漂移。

**12 份真实语料的抽样对照**（按体积分层随机抽，覆盖标准 / 规程 / 公文 / 汇编；
两档**各跑独立进程**并用 CPU 时间，否则 ONNX Runtime 的常驻线程池会污染计时）：

| 指标 | `layout` | `rule` | |
|---|---|---|---|
| 转换 CPU 合计 | 708.3 s | **45.5 s** | 快 **15.6 倍** |
| 正文字数 | 384173 | 353985 | 覆盖 **92%** |
| 标题总数 | 1001 | 5516 | rule 召回高得多 |
| 表格行数 | 3088 | 1067 | **rule 明显少** |

三点需要知道的：

1. **字数差的那 8% 主要不是丢内容，而是表格转 Markdown 的形式差异。** 以
   `遂信联办〔2024〕7号…通知` 为例：`layout` 把「修复信息类型 / 责任部门 / 咨询电话」
   那张**无边框**表转成了 GFM 表格（`|` 与 `---` 就占掉三千多字符），`rule` 认不出
   无边框表，但内容一行没少 —— 变成「`### （三）行政处罚信息修复责任部门` + 正文」。
2. **表格是 rule 档的真短板**：有框线的走 `page.find_tables()`（纯几何）没问题，
   无边框表靠启发式（≥3 行 ≥3 列 + 列左边界对齐），命中率有限。**要表格结构完整就用
   `layout`**。
3. **标题是「召回换准确」**：rule 多出的标题里既有真层级（`一、` / `（一）` / `3.2`），
   也有封面、目录、印章碎片这类噪声。`doc2md/pdf_zhdoc.py` 已经把发文字号、成文日期、
   标准发布/实施日期、纯编号行等明显的噪声挡掉，但做不到 100%。

> ⚠ 两档产出的 md **结构不同**，同一批语料不要混用。
> 换了引擎后想重转某一类文件：`doc2md run --redo-engine pdf-text`
> （只清状态库里该引擎的记录，**不删**已产出的 md，重转时覆盖）。

`rule` 档不加载那 49 MB 版面模型，启动也快得多。若**确定不再用 `layout` 档**，可以在
`doc2md.spec` 里再排掉 `pymupdf4llm` / `onnxruntime` / `numpy` 和模型数据来瘦身 ——
在那之前不要排，排了 `layout` 档整类 PDF 会转不出来。

### 关于 `on_collision`（同名冲突）

`x.docx` 与 `x.pdf` 是同一份公文的两种格式时会算出同一个 `x.md`。

- **`stable`（默认）** —— 先到先得 + 归属粘住：谁先拿到哪个名字就永久不变，
  重转只覆盖自己那一份，**绝不改名、绝不多冒第三个文件**，内容一个字不丢。
- `overwrite` —— 一个名字只剩一个文件。代价：同一文档两种格式内容不同时会丢一半。
- `suffix` —— 旧行为，每次重转重新抢，名字会漂移。**不建议**。

## 云端 OCR 多后端链路


链路在 `config.json` 的 `ocr.backends` 里，按 `priority` 从小到大尝试。
每家后端带独立熔断器（`closed → open → half_open`），冷却时间随失败次数翻倍，
半开时只放一个探测请求。

故障分类决定"熔断 + 切换"还是"直接快速失败"：

| 错误类型 | 熔断该后端 | 切换下一后端 |
|---|---|---|
| 配额用尽 / 鉴权失败 / 背压耗尽 / 网络不可达 | 是 | 是 |
| 能力不足（超页数、超体积、格式不支持） | 否 | 是 |
| 文档本身损坏 | 否 | 否 |
| 空结果 | 否 | 是 |

**只接 OCR 专用服务，不掺通用视觉对话模型。** 通用 VLM 不做逐字复现：密排小字
（发文号、日期、条款编号）在视觉 token 压缩后容易丢或错，还会出现数字幻觉 ——
把〔2024〕15 号读成 13 号这种错误在 md 里看不出任何异常，归档后极难发现。
要往链路上加后端，先确认它是**专用 OCR 模型**且**支持 PDF 或逐页图片输入**。

凭据统一放 `.env`，取值优先级：**系统环境变量 > `.env` > `config.json`**。
跨厂商后端**不继承**彼此的 Token 与服务地址（这条踩过坑：Paddle 的 Token 被
继承给 MinerU 轻量接口 → 401 → 该后端被永久熔断，备用通道形同虚设）。

### 填 Token 不用手改文件

图形界面里点 **工具栏「设置…」→「云端 OCR Token」页** 即可（详见上文「图形界面」一节）。
它会写**真正生效的那个** `.env`，保存后当场生效，并且保留文件里原有的注释与
你自定义的键。命令行侧用 `python -m doc2md env` 查看填写情况（只显示脱敏值）。

界面能填的键与命令行认得的键来自 `config.CRED_FIELDS` **同一份定义**，
不存在"界面里填好了、命令行说没配"的错位。

## 目录结构


```
doc2md/
├── doc2md/                 # 主程序包
│   ├── cli.py              #   命令行入口与各子命令
│   ├── engine.py           #   调度核心：计划、路由、断点续传、md 落盘
│   ├── converters.py       #   本地格式转换 + Markdown 清洗与段落重组
│   ├── pdf_zhdoc.py        #   pdf_engine=rule：接入 vendor/ZhDocParser 的提取器
│   ├── detect.py           #   真实格式嗅探 + PDF 文本层可信度判定
│   ├── ocr_router.py       #   多后端路由器：优先级链路 + 熔断切换
│   ├── ocr.py              #   PaddleOCR 客户端 + 错误分类
│   ├── mineru.py           #   MinerU 客户端（precision / agent 两种模式）
│   ├── vlm.py              #   通用 OpenAI 兼容视觉模型客户端（接专用 OCR 模型）
│   ├── local_ocr.py        #   本地 RapidOCR 子进程客户端（可选）
│   ├── com.py              #   .doc/.xls 转换（Windows COM / 其他平台 LibreOffice）
│   ├── watcher.py          #   实时监控模式
│   ├── config.py           #   配置加载 + .env 凭据注入
│   └── state.py            #   SQLite 状态库（断点续传的依据）
├── vendor/                 # 第三方子项目（git subtree 引入，见 vendor/README.md）
│   └── ZhDocParser/        #   纯规则 PDF 结构还原（MIT）；只取它的 PdfExtractor
├── tests/                  # 回归 / 集成测试
├── scripts/                # 运维脚本（清理、体检、隔离，默认 dry-run）
├── installer/              # 安装 / 卸载程序
│   ├── installer_app.py    #   安装程序本体（自解压 + Tk 界面 + 提权 + 建快捷方式）
│   ├── installer.spec      #   PyInstaller 配置（onefile，载荷内嵌）
│   ├── uninstall_app.py    #   卸载程序本体（Tk 界面 + 静默 + 自删除）
│   ├── uninstaller.spec    #   PyInstaller 配置（onefile，独立，不依赖 _internal）
│   └── uninstall.bat       #   卸载兜底脚本（万一 exe 被安全软件拦下）
├── devkit.py               # 开发辅助：从 config.json 的 roots 自动挑样例
├── gui.py                  # 图形界面（Tkinter，零新增依赖）
├── launcher.py             # 菜单式启动器（被 文档转MD.bat 调用）
├── app.py                  # 统一入口：带参数走命令行、无参数进菜单或图形界面
├── make_installer.py       # 打包编排：应用 → 载荷 → 单文件安装程序
├── doc2md.spec             # PyInstaller 打包配置（一份 Analysis 造两个 exe）
├── 打包安装包.bat          # 双击生成 dist-installer\doc2md-安装程序.exe
├── 打包EXE.bat             # 双击只生成 dist\doc2md\ 下的两个 exe
├── 文档转MD-GUI.bat        # 双击打开图形界面
├── config.example.json     # 配置模板（提交进仓库；真正的 config.json 被忽略）
├── .env.example            # 凭据模板（同上）
├── install.sh              # Linux/macOS 一键安装（有单文件二进制就秒装，否则源码装 venv）
├── build_linux.sh          # Linux 打包（onedir 目录 / onefile 单文件）
├── docs/                   # 文档与截图（USAGE.md 是完整使用说明）
└── requirements.txt
```

> `.venv` 与 `.venv-gui` 都是本地环境、都不进版本库。前者是主环境；
> 后者只在**跑/打包图形界面**时需要，原因见 [图形界面（GUI）](#图形界面gui)。

## 打包成 Linux 可执行文件

不打虚拟环境、不装 Python 也能用：PyInstaller 打成独立可执行文件（ELF x86-64）。

```bash
bash build_linux.sh            # onedir（默认）：dist/doc2md/ 目录，启动最快
bash build_linux.sh onefile    # 单文件：dist-onefile/doc2md，一个文件便于分发
bash build_linux.sh both
```

| 形态 | 产物 | 启动 | 适合 |
|---|---|---|---|
| `onedir` | `dist/doc2md/doc2md` + `_internal/`，221 MB / 239 个文件 | 快 | 本机反复调用；解释器有 tkinter 时还会多一个窗口版 `doc2md-gui` |
| `onefile` | `dist-onefile/doc2md`，单文件 113 MB | 每次启动自解包，冷/热约 1.3 s | 对外分发：只给这一个文件，拷走即用 |

实测（PyInstaller 6.22 / Python 3.11 / 2026-10-03）：两种形态都能独立跑通
docx、xlsx、文本层 PDF、老式 `.doc` / `.xls`（走 LibreOffice `soffice`）与云端 OCR；
`config.json` / `.env` / `state.db` 一律落在**可执行文件旁边**（按 `sys.executable`
解析程序根，不是一次性解包目录）。

几点要知道的：

- **依赖一起打包，外部程序打不进去**：`.doc` / `.xls` 仍要目标机装 LibreOffice，
  本地 RapidOCR 仍要另一个装了 `rapidocr` + `onnxruntime` 的解释器。
- **单文件版的代价**：每次启动把内容解包到 `${TMPDIR:-/tmp}/_MEIxxxxxx`，
  峰值约 213 MB、进程退出即删。`/tmp` 挂 `noexec` 或空间不够会起不来，
  用 `TMPDIR=别的目录` 规避。
- 单文件模式**只出控制台版**，避免把几百 MB 再复制一份进同一个文件。
- 想连窗口版一起打：先让解释器有 tkinter（`apt install python3-tk`），
  再跑 `build_linux.sh`。解释器没有 tkinter 时脚本只打控制台版并明确提示。
- `doc2md.spec` 是平台自适应的：Windows 那条路径原样保留（缺 tkinter 仍然硬中止、
  仍然收集 `pywin32`），只有非 Windows 才改走 LibreOffice 与 POSIX 的 watchdog observer。

## 安装版（单文件安装程序）


不想装 Python、也不想碰命令行时，用构建出来的**单文件安装程序**：

```bat
:: 打包（必须用带 tkinter 的 .venv-gui；或直接双击 打包安装包.bat）
.venv-gui\Scripts\python.exe make_installer.py --force

:: 产物
dist-installer\doc2md-安装程序.exe     ≈ 118 MB，就一个文件
dist-installer\doc2md-payload.zip      裸载荷：解压即用的绿色版
```

> **改了 `gui.py` / `doc2md\` 里的代码，一定要加 `--force`。** 不加的话第 1 步会看到
> `dist\doc2md` 里已有 exe 就跳过，安装包内嵌的还是旧构建 —— 表现就是"源码里加了功能、
> 装出来却没有"。这个坑真踩过：界面上多了「填写云端 OCR Token」，安装版里找不到。

一条命令跑完「应用 → 卸载程序 → 载荷 → 安装包」：

| 步骤 | 做什么 |
|---|---|
| 1 | PyInstaller + `doc2md.spec` → `dist\doc2md\`（两个 exe 共享 `_internal\`） |
| 2 | PyInstaller + `installer\uninstaller.spec` → `build\uninstaller\doc2md-uninstall.exe`（**独立单文件**，卸载时要删 `_internal\`，自己不能依赖它） |
| 3 | 直接把两个 exe + `uninstall.exe` + `_internal\` + `README.md` + `LICENSE` + 示例配置 + `uninstall.bat` 映射进 zip（**无中间暂存目录**，少复制 1000 多个文件） |
| 4 | 压成 `build\doc2md-payload.zip` |
| 5 | PyInstaller + `installer\installer.spec` → 单文件安装程序（载荷内嵌其中） |

> 本机与外发目标都不保证装过 Inno Setup / NSIS / 7-Zip，所以安装程序是
> **只用 Python 标准库 + PyInstaller 自建的自解压包**，零外部工具依赖。
> 第 2 步每次都会重打（改 `uninstall_app.py` 不用加 `--force`）；`--force` 只影响第 1 步。

### 安装程序怎么用

```bat
doc2md-安装程序.exe                      :: 图形界面：选目录、建快捷方式
doc2md-安装程序.exe /S                   :: 静默装到 C:\Program Files\doc2md
doc2md-安装程序.exe /S /D=D:\doc2md      :: 静默装到指定目录
doc2md-安装程序.exe /S /D=... /NOICONS   :: 静默且不建快捷方式
doc2md-安装程序.exe --help
```

- 默认装到 `C:\Program Files\doc2md`。**安装程序带 `requireAdministrator` 清单**，
  双击时 Windows 会直接弹出 UAC 授权框，点「是」即开始安装。
  > 早先试过"清单用 asInvoker、只在真写不进去时用 `ShellExecuteW("runas")` 自我
  > 提权"，实测在真实双击场景下不可靠：提权被推到点「开始安装」之后，那一步没成
  > 就表现为**"调不出 UAC、一直卡在那里"**，还没有任何可读的错误提示。右键「以
  > 管理员身份运行」能绕过，但用户不该被迫知道这件事。
- 装到受保护目录（Program Files / Windows）时，安装程序会自动执行
  `icacls <目录> /grant *S-1-5-32-545:(OI)(CI)M /T`，给 **Users 组补上「修改」权限**。
  这一步不能省：程序运行期要把 `state.db` / `logs\` / `.env` / `config.json` 写在
  **自己所在的目录**里（换机器不用改路径），而受保护目录默认对普通用户只读 ——
  不补权限的话，装完普通双击运行会**存不下配置、建不出 state.db**。
  用户自己挑的普通目录（`D:\doc2md` 之类）本来就可写，不做任何额外放宽。
- 装完在安装目录生成 `config.json` 与 `.env` 模板，**只补缺、不覆盖**：升级重装不会动你的
  配置、凭据和 `state.db`。
- **装完第一次打开程序会把「设置」窗口直接开到「云端 OCR Token」页**（因为一个 Token 都
  还没填）。不填也能用 —— 点「稍后再说」，docx / xlsx / 有文字层的 PDF 照常转；这个记号
  会写进 `.env` 的注释里，之后不再打扰，需要时从菜单或工具栏随时再打开。
- 卸载：安装目录里的 **`uninstall.exe`**（图形界面），或「设置 → 应用」里的条目 ——
  那条登记项的 `UninstallString` 就指向这个 exe。在 Program Files 下卸载会自动请求提权。
  详见下一节。

### 卸载

安装目录里的 `uninstall.exe`，「设置 → 应用」里的条目也指向它：

```bat
uninstall.exe                  :: 图形界面：显示将删除的目录，确认后执行
uninstall.exe /S               :: 静默（脚本/自动化用）
uninstall.exe /D=<安装目录>     :: 指定目录（默认取本程序所在目录）
```

会删掉：安装目录**及其全部内容**（含 `config.json`、`.env`、`state.db`、`logs\`）、
桌面与开始菜单的快捷方式、「设置 → 应用」的登记项。
> `state.db` 是断点续传的依据，删掉后重装需要从头重转。要保留就先把
> `state.db`（以及 `config.json` / `.env`）拷出去。

`uninstall.bat` 仍然保留在同一目录，作为**兜底**（万一 exe 被安全软件拦下）。
设计上的两个要点：

- **为什么要先把自己复制到 `%TEMP%` 再干活。** Windows 不允许删除正在运行的程序文件，
  而卸载程序自己就躺在安装目录里 —— 不搬走的话 `uninstall.exe` 删不掉、目录也删不干净。
  于是启动时先把自己复制到 `%TEMP%\doc2md-uninstall-<pid>.exe` 交给副本（副本由已提权的
  父进程启动，**不会再弹一次 UAC**），父进程立刻退出，由副本完成删除（**包括目录里的原
  exe**）。副本自己的文件登记到重启时清理。
  > **踩过的坑（已实测排除）**：先试的是"把除自己以外都删掉，再交给一个游离的 `cmd`
  > 循环 `rd /s /q` 延时重试"。**走不通** —— 即使持有句柄的进程已经退出、同一刻在
  > Python 里 `os.unlink()` 同一个文件**成功**，`rd /s /q` 仍会一直报
  > 「另一个程序正在使用此文件，进程无法访问」，直到循环跑满都不恢复。
  > 所以代码里**没有**任何依赖 cmd 延时重试删除的路径，`tests/test_uninstaller.py` 盯着这一点。
- 代价：`/S` 静默卸载**返回得比较早**（交接完即返回），真正干活的是后台副本，过程写在
  `%TEMP%\doc2md-uninstall.log`。图形界面模式无感。

### 安装后目录长什么样

```
C:\Program Files\doc2md\
├─ doc2md.exe            控制台版（命令行 / 中文菜单）
├─ doc2md-gui.exe        窗口版（图形界面）
├─ uninstall.exe         卸载（图形界面；「设置 → 应用」里的条目也指向它）
├─ README.md             ← 项目说明（完整使用说明在 docs\USAGE.md）
├─ docs\USAGE.md          ← 完整使用说明（本文件的副本）
├─ LICENSE
├─ config.json           首次安装自动生成（来自 config.example.json）
├─ config.example.json
├─ .env                  首次安装自动生成（来自 .env.example，值是空的）
│                        打开程序会提示填 Token，也可以直接编辑本文件
├─ .env.example
├─ uninstall.bat         卸载兜底脚本（exe 被拦时用）
└─ _internal\            约 203 MB 运行时（Python / Tk / pymupdf + 49 MB 版面模型 …），别删
```

### 供自动化 / 智能体（LLM harness）调用

安装后**不需要装 Python，也不需要配任何环境变量**。给自动化程序或大模型 harness 的调用约定：

| 项 | 值 |
|---|---|
| 可执行文件 | `<安装目录>\doc2md.exe`（默认 `C:\Program Files\doc2md\doc2md.exe`） |
| 调用形式 | `doc2md.exe <子命令> [公共参数]`，与 `python -m doc2md <子命令>` 完全等价 |
| 交互性 | 所有子命令**都不需要 TTY 输入**；输出为 UTF-8 文本；退出码 `0` 表示成功 |
| 能力发现 | `doc2md.exe --help` 列出全部子命令与参数 |
| 文档 | `<安装目录>\README.md`（项目说明）、`<安装目录>\docs\USAGE.md`（完整使用说明） |
| 配置 / 凭据 | `<安装目录>\config.json`、`<安装目录>\.env` |
| 状态库 | `<安装目录>\state.db`（断点续传唯一依据，删掉会全量重转） |
| 路径解析 | 一切相对 **exe 所在位置**，不依赖当前工作目录 |

> **请用 `doc2md.exe` 而不是 `doc2md-gui.exe`。** 两者其实是同一份代码、命令行接口
> 完全一样（只要带参数就走 CLI，与是哪个 exe 无关）；但 `doc2md-gui.exe` 是 GUI 子系统、
> 不自带控制台，输出得靠重定向或管道才拿得到。脚本化调用一律用 `doc2md.exe`。

典型调用：

```bat
set D=C:\Program Files\doc2md
"%D%\doc2md.exe" scan   --root "E:\语料"    :: 试运行，不写任何文件
"%D%\doc2md.exe" run    --root "E:\语料"    :: 正式转换，中断可续传
"%D%\doc2md.exe" status                     :: 统计与各后端今日配额
"%D%\doc2md.exe" ping                       :: 云端 OCR 后端连通性
```

## 打包成 Windows EXE


> 想要**单文件安装包**（而不是裸的 `dist\doc2md\` 文件夹），见
> [安装版（单文件安装程序）](#安装版单文件安装程序) —— `make_installer.py` 正是在
> 这一节的基础上再套一层自解压。

目标机器不想装 Python 时，把程序打成独立可执行文件：

> ⚠ **改过任何 `.bat` 之后，先确认行尾是 CRLF。** cmd 遇到 `goto`/标签跳转时按字节偏移
> 重新定位文件指针，纯 LF 的批处理会让它错位到行中间、**把行的尾巴当命令执行** —— 屏幕上
> 刷一堆 `'m' 不是内部或外部命令`、`系统找不到指定的文件。`，但**构建照常成功**，只看
> `[DONE]` 是发现不了的。而且 `.gitattributes` 里的 `*.bat text eol=crlf` **只在 checkout
> 时生效**：文件被工具重写成 LF 之后，git 归一化比较认为内容相同，`git status` / `git diff`
> 都看不出问题，能一路混进发布包（`installer/uninstall.bat` 尤其危险，它要跑到用户机上）。
> `tests\test_packaging_invariants.py` 的第 `[5]` 节会拦住这种文件。

```bat
:: 双击 打包EXE.bat 即可；等价命令是：
.venv-gui\Scripts\python.exe -m pip install pyinstaller
.venv-gui\Scripts\python.exe -m PyInstaller doc2md.spec --noconfirm
```

产物在 `dist\doc2md\`，里面有**两个** exe：

| 产物 | 双击后 | 用途 |
|---|---|---|
| `doc2md.exe` | 中文控制台菜单 | 原来那套；带参数时等价于 `python -m doc2md <参数>` |
| `doc2md-gui.exe` | 图形界面 | 窗口版；`--menu` 可强制走菜单 |

两者**共享同一个 `_internal\` 目录** —— 一份 `Analysis`/`PYZ` 造两个 `EXE` 对象
再一起 `COLLECT`，所以多带一个窗口版只多几 MB，不是把 203 MB 再复制一份。
各自的显式开关：`doc2md.exe --gui` 走界面，`doc2md-gui.exe --menu` 走菜单。

> **必须用带 tkinter 的解释器打包。** PyInstaller 只能打包**构建解释器实际拥有**的
> 东西；用精简版 Python 3.13 打包，`tkinter` 根本不会被打进去，而**打包时不会报错**，
> 只在用户双击 `doc2md-gui.exe` 时才崩。所以 `doc2md.spec` 开头加了一道前置自检，
> 没有 tkinter 就直接中止并提示改用 `.venv-gui`；`打包EXE.bat` 也会优先挑 `.venv-gui`。

### 打包最容易漏的东西：第三方库的数据文件

PyInstaller 只做**静态分析**。import 链它跟得住，但第三方包**运行时才去读**的
数据文件必须显式声明 —— 否则打出来的包看着一切正常，跑起来才炸。本项目踩过一次
大的：一次全量跑，3809 个文件里 **292 个失败**。

```
FileNotFoundError: ...\_internal\pymupdf\layout/resources/onnx/layout_rf2.4.1+imf1.yaml
AttributeError: module 'pymupdf4llm' has no attribute 'to_markdown'
```

`pymupdf-layout` 是个**独立的发行包**，它不经 pip 的常规路径落文件，而是把版面
模型直接塞进 `pymupdf/layout/` 目录里。PyInstaller 按 `pymupdf` 自己的清单收集，
看不见这些「外来」文件 —— 于是 `import pymupdf4llm` 时
`pymupdf.layout.activate()` → `BoxRFDGNN.__init__` 一 `open()` 模型 YAML 就失败。
后果是**所有带文字层的 PDF 全部转不出来**（那 292 个正好是 `pdf_text` 一整类）。

那 27 个报 `AttributeError` 的是**同一个根因的另一种表现**：首次导入失败后，半初始
化的模块被留在 `sys.modules` 里，第二次访问就成了「没有这个属性」。看到两种毫不
相干的报错混在一起，先怀疑是不是同一个底层故障。

修复就一行（`doc2md.spec`）：

```python
datas += collect_data_files("pymupdf", subdir="layout")   # 约 49 MB
```

**不要只挑几个 `.onnx` 图省事**：表格网格模型有 10 个版本、feature_set 有 3 种组合，
少一个就是某一类版面在运行期静默失败 —— 正是这次要修掉的故障形态。

> 排查同类问题的手法：拿**源码态**和**打包态**跑同一个文件，行为不一致就往数据
> 文件上想。反过来说，`python -c "import 某库"` 在源码里成功、在 exe 里失败，
> 也是同一个信号。

### 分发时的目录约定

- **整个 `dist\doc2md` 文件夹一起拷**，不能只拷 exe —— 依赖都在 `_internal\` 里。
- `config.json` / `.env` 放在 exe **旁边**；首次运行会自动生成配置模板。
- 程序根目录按 **exe 所在位置**解析，所以 `state.db`（断点续传依据）和 `logs\`
  都落在 exe 旁边、跟着程序走，**不会**像解包目录那样一退出就丢。
- 两个 exe 读同一份 `config.json` / `.env` / `state.db`，混用不会串味。
- `config.json` 里的 `state_db`、`log_dir`、`roots`、`local_ocr.python_exe`
  若写了绝对路径，换机器时要相应修改。

### 打不进去的两件事（能力边界，不是缺陷）

| 能力 | 原因 |
|---|---|
| `.doc` / `.xls` / `.wps` / `.et` | Windows 走本机 WPS/Office 的 COM，Linux 走 LibreOffice；**目标机必须装其中之一** |
| 本地 RapidOCR | 需要另一套装了 `rapidocr` + `onnxruntime` 的解释器，用 `local_ocr.python_exe` 指过去 |

云端 OCR 链路（paddle / mineru / siliconflow）不受影响，Token 照旧放 `.env`。

### 体积

实测（PyInstaller 6.22 / Python 3.12 / onedir，2026-10-03）：**约 203 MB、1039 个
文件**，其中两个 exe 各约 10 MB，其余全在 `_internal\`：

| 内容 | 体积 | 说明 |
|---|---|---|
| `pymupdf` | 87 MB | PDF 渲染 + 文本层抽取；**其中 49 MB 是 `layout/` 下的 ONNX 版面模型**，见上一节 |
| `onnxruntime` | 36 MB | 跑上面那个版面模型用的推理引擎 |
| `numpy`（含 `numpy.libs`） | 27 MB | onnxruntime 的依赖 |
| `libcrypto` / `libssl` | 9 MB | `requests` 的 TLS，云端 OCR 必需 |
| tkinter 运行时 | 9 MB | `_tkinter.pyd` + `tcl86t`/`tk86t` + tcl/tk 数据，**文件数的大头是这里的小文件** |
| `python312.dll` + `base_library.zip` | 8 MB | 解释器本体 |
| 其余 | 约 27 MB | `pywin32`、`sqlite3.dll`、`charset_normalizer` 等 |

窗口版只多占几 MB —— 两个 exe **共享同一个 `_internal\`**，不是把 203 MB 复制一份。
单文件安装包 `doc2md-安装程序.exe` 是 **118 MB**（载荷 203 MB 压到 109 MB）。

> **别拿 `pymupdf` / `onnxruntime` / `numpy` 换体积。** 早期文档写过「在 `excludes`
> 里加上这三个可省 60 MB」—— 那是错的，而且是**最危险的那种错**：`pymupdf4llm` 1.28
> 起版面模型**随导入即启用**，配置里关不掉（`pdf_use_layout` 已废弃、是个单向开关）。
> 去掉这三样，所有带文字层的 PDF 会当场全部转不出来 —— 正是 2026-10-03 那次 292 个
> 失败的原因。
>
> **唯一正当的瘦身路径是把 `pdf_engine` 定成 `rule`**（纯规则引擎，见「配置要点」），
> 让它不再需要这条链路 —— `rule` 档不 import `pymupdf4llm`，也就永远不加载
> onnxruntime。此时可以同时做两件事：
> ① `doc2md.spec` 的 `excludes` 里加上 `onnxruntime`、`numpy`；
> ② 删掉那行 `collect_data_files("pymupdf", subdir="layout")`。
> 两处都做完，`_internal\` 瘦掉约 **106 MB**（49 MB 模型 + 36 MB onnxruntime +
> 21 MB numpy.libs），安装包从 118 MB 降到 40 MB 上下。
> **只做一半不行** —— 只删 `collect_data_files` 却不切 `rule`，就正好踩回上面那个
> 292 失败的坑。

**注意别把 `tkinter` 加进 `excludes`**，否则窗口版会启动即崩。

### 控制台报错却看不到错误：先把编码放宽

Windows 控制台默认 GBK，而界面文案里用了 `⭐ ⚠ ↳ ↔ ✓ ✗ ⊘` 这类符号。一行
`print("⭐ ...")` 编不进 GBK 就抛 `UnicodeEncodeError` —— 更糟的是它发生在
**打印报错信息的那一刻**：引擎要打 `      ↳ FileNotFoundError: ...`，`↳` 编不出去，
异常盖住了原始异常，屏幕上只剩：

```
[PYI-21272:ERROR] Failed to execute script 'app' due to unhandled exception!
```

查一个「文本层 PDF 全失败」的问题，却被引去翻编码，白绕一圈。（`文档转MD.bat`
双击走控制台菜单，菜单里正好有个 `⭐`，所以这条路以前是**必崩**的。）

现在四个入口 —— `app.py`（打包入口）、`launcher.py`（bat 直接调它，绕过 app.py）、
`cli.py`、`gui.py` —— 都在做第一件事时调 `doc2md.stdio.make_stdio_safe()`，把
`stdout` / `stderr` 的错误策略放宽成 `replace`：编不出的字符显示成 `?`，但绝不抛
异常、绝不掩盖原始错误。顺带把 `launcher.MENU` 里的 `⭐` 换成了 GBK 里有的 `★`。

> 单独做成 `doc2md/stdio.py` 而不是塞进 `cli.py`：后者会顺手把整个引擎拖进来，
> 图形界面「秒开」就没了。

### 打包适配了什么

源码运行时行为完全不变，只是让打包后的路径与子进程假设成立：

- `config.py` / `__main__.py` / `launcher.py` 在 `sys.frozen` 下改用
  **exe 所在目录**作为程序根目录 —— 否则 `config.json`、`state.db`
  会被落到一次性解包目录里，退出即删，断点续传静默失效。
- `launcher.py` 打包后**不再起子进程**：那时 `sys.executable` 就是 exe 自己，
  再拼 `-m doc2md` 会无限自我递归；改为同进程直接调用 CLI。
- `doc2md.spec` 把 `config.example.json` 打进包里；`config.bootstrap_config()`
  在 exe 旁边找不到示例时会**回退到包内那份**，保证首次运行能自动生成配置。
- `app.py` 负责在菜单 / 图形界面 / 命令行之间分发：有子命令走 CLI，
  无子命令时看「文件名里有没有 gui」和「有没有真实控制台」——窗口版 exe
  的 `stdout` 不是真实句柄，据此认出自己该开图形界面。
- `engine.py` 多了一个可选的 `should_stop` 回调（GUI 的「停止」按钮用），
  CLI 不传就是 `None`，行为与以前完全一致。
- 四个入口 `app.py` / `launcher.py` / `cli.py` / `gui.py` 都先调
  `doc2md.stdio.make_stdio_safe()` 再输出（原因见上一节）。
- `make_installer.py` 重建前把 `dist\doc2md` 与 PyInstaller 的 `workpath`
  **改名挪开**而不是删除：装了终端安全软件的机器会拦截「一次删上千个文件」，
  PyInstaller 自己清目录时会撞上、构建当场中止；改名只动目录项，又快又稳。
  挪开的东西统一带 `.old-<时间戳>` 后缀，攒多了集中清理即可。

## 开发与测试


```bat
:: 策略测试：151 项断言，假后端，不联网、不耗配额
.venv\Scripts\python.exe tests\test_ocr_router.py

:: 输出命名策略端到端（需要一个真实 docx + xlsx 做样本）
.venv\Scripts\python.exe tests\test_output_naming.py

:: 空文档 / 加密文件的归类（夹具现场构造，不依赖个人语料）
.venv\Scripts\python.exe tests\test_encrypted_empty.py

:: 凭据文件读写：保留注释、引号转义、置空删除、写完即时生效
.venv\Scripts\python.exe tests\test_token_env.py

:: 转换清单（convert）：编码探测、缺失项上报、--force 语义、清单驱动不扫 roots
.venv\Scripts\python.exe tests\test_cli_convert.py

:: 文本型 PDF 的 rule 档接入层（vendor/ZhDocParser 的适配与规则）
.venv\Scripts\python.exe tests\test_pdf_zhdoc.py

:: 安装程序：提权清单、参数解析、卸载入口（离线，不弹 UAC、不动系统）
.venv\Scripts\python.exe tests\test_installer_logic.py

:: 卸载程序：沙箱里造一棵同构的安装目录真删一遍；快捷方式/注册表被替换成哑实现
.venv\Scripts\python.exe tests\test_uninstaller.py

:: 「设置」窗口端到端（含 Token 页、可滚与按钮常驻、引擎选档；要桌面会话，看不到界面时自动跳过）
.venv-gui\Scripts\python.exe tests\gui_token_smoke.py

:: 全链路冒烟：故意把链路首端设成坏后端，验证熔断切换（联网、会消耗 MinerU 额度）
.venv\Scripts\python.exe tests\test_engine_smoke.py
```

测试脚本**不写死任何语料路径**：样例由 `devkit.py` 从 `config.json` 的 `roots`
里自动挑（冒烟测试还会用引擎同一套判定筛出"真的会走 OCR"的 PDF）。找不到样例
时打印 `[跳过]` 并以退出码 2 结束，不会误报失败。也可以用环境变量固定样本：
`DOC2MD_SAMPLE_DOCX`、`DOC2MD_SAMPLE_XLSX`、`DOC2MD_SMOKE_PDFS`、
`DOC2MD_MINERU_PDFS`（多路径用 `os.pathsep` 分隔）。

`scripts/` 下的运维脚本**默认 dry-run，只打印不动作**，确认无误再加 `--apply`
或 `--move`；涉及删除的一律先移到隔离目录而不是直接删。

## 空文档与加密文件：为什么不算「失败」


统计里有一类文件**永远转不出东西，但也不该躺在失败列表里**。它们有个共同点：
问题出在**源文件本身**，重试一万次结果也一样。现在它们统一记成 `skipped`：

| 情况 | 怎么认出来的 | 归类 |
|---|---|---|
| **空壳文档** —— WPS 会存出结构完整、正文却一个字符都没有的 docx | 包内 `word/` 下任何部件都没有 `<w:t>` 文本标签，也没有任何媒体文件 | `skipped`：文档为空（无正文文本、无插图） |
| **加密的 OOXML** —— .docx 设了「打开密码」，外层其实是 OLE2，里面装着 `EncryptedPackage` + `EncryptionInfo` | 解析 OLE2 目录项，见到这两个标志流；结构读不动时退回按 UTF-16LE 关键字扫描 | `skipped`：文件已加密，需先去掉密码 |
| **加密的老式 .doc / .xls** —— 格式没变，靠标记位声明有密码 | Word 读 FIB 的 `fEncrypted` 位；Excel 走 BIFF 的 `FILEPASS` 记录 | 同上 |

为什么要费劲去认：不认的话，加密的 `.docx` 会被 `sniff()` 按扩展名当成老 doc
送进转换器（Windows 的 COM / Linux 的 soffice），WPS 回一句「文档打开失败」—— 看着像文件损坏，其实只差一个密码；
然后它每次 `run` / `retry` 都再报一次，把真正需要处理的问题淹掉。

识别逻辑全在 `detect.py`（`CfbReader` / `encryption_reason` / `docx_is_empty`），
**零外部依赖**（自己实现了一个最小的 OLE2/CFB 目录解析器，不引 `olefile`）。
加密文件在**分流阶段就被拦下**，连慢且必报错的 COM 都不会去调；
万一还有漏网的（`.wps`/`.et`、IRM 保护等），`_run_com` 的异常分类会兜底 ——
错误信息里带「密码 / 加密 / protected」的一律按加密归类，而不是记失败。

反过来，这两种情况**必须仍然算失败**，别被上面误伤：文档读得动、但解析确实报错的
（格式损坏、COM 异常），以及 OCR 后端返回空结果的。

对应测试：`tests/test_encrypted_empty.py`（夹具全部现场构造，不依赖个人语料）。

## 注意事项


- **改了代码对已在运行的进程不生效** —— 监控模式要重启才是新逻辑。
- `config.json` 的 `output` 段支持热加载（约 2 秒生效），`ocr` 段的改动需要重启。
- `state.db` 是断点续传的唯一依据，**不要提交、也不要随意删除**；想重转某批
  文件，从状态库里删掉对应记录即可（`scripts/` 里有现成的工具）。
- `.doc` / `.xls` 的转换需要本机有转换器：Windows 用 WPS/Office 的 COM，Linux 用 LibreOffice；都没有时会走降级路径。
- 本地 OCR 需要**另一个**装了 `rapidocr` + `onnxruntime` 的 Python 环境，
  在 `local_ocr.python_exe` 里指过去；不装也不影响，云端链路能覆盖。
- **图形界面需要 .venv-gui**（带 tkinter 的完整 Python），`.venv` 跑不了 ——
  原因见 [图形界面（GUI）](#图形界面gui)。
