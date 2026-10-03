# doc2md · 文档批量转 Markdown

把一整个目录树的 **docx / doc / xls / xlsx / pdf** 批量转成 Markdown，并带上
**断点续传**、**实时监控**、**扫描件 OCR（多云端后端自动熔断切换）** 三件事。

为中文公文归档场景做的：段落重组、页码过滤、标题识别、落款分行、表格还原、
敏感目录不上云。

```
┌── 本地直转（不联网、最快） ────────────────────────────────┐
│  .docx  → mammoth        .xlsx → openpyxl                 │
│  .doc/.xls → Office COM（Windows）                        │
│            → LibreOffice soffice（Linux）                 │
│  有文本层 PDF → pymupdf4llm（带可信度复核，防"假文本层"）  │
└───────────────────────────────────────────────────────────┘
┌── 扫描件 / 无文本层 PDF → OCR ─────────────────────────────┐
│  1. paddle          PaddleOCR-VL      专用、出插图         │
│  2. mineru[精度]    MinerU precision  专用、出插图、可分段 │
│  3. sf-deepseek-ocr DeepSeek-OCR      专用、一问一答无队列 │
│  4. mineru[轻量]    MinerU agent      免 Token 兜底        │
│  任一层配额用尽 / 背压 / 鉴权失败 → 熔断该后端并自动切换   │
└───────────────────────────────────────────────────────────┘
```

## 平台支持

Windows 与 Linux 都可用；macOS 未验证。两者差异只在老式 `.doc` / `.xls` 的转换通道与打包产物上，
其余功能（断点续传、监控、云端 OCR、空文档与加密文件判定）一致。

| 事项 | Windows | Linux |
|---|---|---|
| 老式 `.doc` / `.xls` / `.wps` / `.et` | 本机 Office / WPS 的 COM | LibreOffice `soffice --headless`（需自行安装） |
| 依赖声明 | `pywin32`（`sys_platform == "win32"` 条件安装） | 系统包 `libreoffice-writer` + `libreoffice-calc` |
| 启动入口 | `.bat` 菜单 / GUI / 命令行 | `doc2md` 命令（即 `python -m doc2md ...`） |
| 一键安装 | `doc2md-安装程序.exe`（免装 Python） | `bash install.sh`（二进制秒装 / 源码装 venv） |
| 打包产物 | `dist\doc2md\`（两个 exe 共享 `_internal\`） | `dist/doc2md/`（目录）或 `dist-onefile/doc2md`（单文件） |

代码里的分叉点只有三处：

- `doc2md/com.py`：`to_ooxml()` 在 `os.name != "nt"` 时走 LibreOffice 分支，Windows 的 COM 原路径不变。
- `doc2md/detect.py`：OLE2 类型判定先扫前 16KB，未认出 `WordDocument` 时用内置 `CfbReader` 精确解析目录项。
  原因：LibreOffice 写出的 `.doc` 目录扇区落在 16KB 之后，老办法会被流数据里的巧合字节 `Book` 误判成 xls。
- `requirements.txt`：`pydantic` 是 `vendor/ZhDocParser`（rule 档 PDF 引擎）的直接依赖，单独列出。

## 安装

三种方式，按场景挑一个。**装完先做三件事**：① 首次运行自动生成 `config.json`，
把里面的 `roots` 改成你的语料目录；② 在 `.env` 里填云端 OCR 凭据（不填也能转
docx / xlsx / 有文字层的 PDF）；③ 跑一次 `doc2md scan` 试运行 —— 看清每个文件会走
哪条通道，不写任何文件。

### Windows

方式一：**单文件安装程序**（目标机不用装 Python；Release 里只有 Linux 包，Windows
装包在本机跑一次 `打包安装包.bat` 生成，见 [使用说明](docs/USAGE.md#安装版单文件安装程序)）

```bat
doc2md-安装程序.exe                  :: 双击：图形界面，选目录、建快捷方式
doc2md-安装程序.exe /S               :: 静默装到 C:\Program Files\doc2md
doc2md-安装程序.exe /S /D=D:\doc2md  :: 静默装到指定目录
```

装完安装目录里有 `doc2md.exe`（命令行 / 中文菜单）与 `doc2md-gui.exe`（图形界面），
卸载用它自带的 `uninstall.exe`。构建与更多参数见
[使用说明 → 安装版](docs/USAGE.md#安装版单文件安装程序)。

方式二：**源码 + 虚拟环境**（开发用）

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.json
.venv\Scripts\python.exe -m doc2md scan
```

不想敲命令就双击 `文档转MD.bat`（中文菜单）或 `文档转MD-GUI.bat`（图形界面）。

### Linux / macOS

一条命令装完（推荐；自动取 Release 里的现成程序，不需要 Python）：

```bash
curl -fsSL https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh | bash
```

用 `wget` 也行；要传参数就在后面加 `-s --`：

```bash
wget -qO- https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh | bash
curl -fsSL https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh \
  | bash -s -- --prefix /opt/doc2md
```

默认装到 `~/.local`，装完就有 `doc2md` 命令。程序约 130MB，下完会按 Release 的
`SHA256SUMS-linux.txt` 校验，对不上会直接停下。可用的参数：`--version v1.0.2`
（默认最新版）、`--gh-proxy https://gh-proxy.com/`（GitHub 慢时套加速）、
`--bin 文件`（用你已下好的 Release 程序）、`--source`（改成 git clone 源码安装）、
`--uninstall`。全部参数与环境变量见[使用说明](docs/USAGE.md#linux--macos一条命令--一键脚本--源码)。

现成程序按 glibc 2.36 链接（在 Debian 12 上打包），Debian 12 / Ubuntu 23.04 及更新的系统
直接可用；更旧的系统会报 `GLIBC_2.xx not found`，脚本遇到会**自动改用源码安装**重来一遍
（需要 git 与 Python 3.11+），也可以自己加 `--source`。

国内直连 GitHub 常常很慢甚至卡死（脚本自己取程序那段也一样）。这时套个加速前缀，
取脚本和后面下程序都走这条通道：

```bash
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh \
  | bash -s -- --gh-proxy https://gh-proxy.com/
```

已经手动下好 Release 里的程序时，可以跳过下载：

```bash
curl -fsSL https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh \
  | bash -s -- --bin ~/下载/doc2md-v1.0.2-linux-x86_64
```

仓库已经克隆到本地时，也可以就地跑脚本：

```bash
# 老式 .doc / .xls 才需要 LibreOffice（Debian / Ubuntu）
sudo apt install -y libreoffice-writer libreoffice-calc

bash install.sh                 # 装到 ~/.local，装完就有 doc2md 命令
bash install.sh --prefix /opt/doc2md
bash install.sh --source        # 强制源码方式（建 venv + 装依赖）
bash install.sh --uninstall     # 卸载
```

脚本会优先用仓库里打包好的**单文件可执行程序**（秒装、不需要 Python）；没有就退回
源码方式。程序把自己的 `config.json` / `.env` / `state.db` 写在安装目录的
`share/doc2md/` 里，跟着程序走，不依赖当前工作目录。

源码 + 虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
.venv/bin/python -m doc2md scan
```

方式三：**打成单个可执行文件分发**（目标机不装 Python）

```bash
bash build_linux.sh onefile     # 产物：dist-onefile/doc2md —— 一个文件拷走即用
```

详见 [使用说明 → 打包成 Linux 可执行文件](docs/USAGE.md#打包成-linux-可执行文件)。

## 常用命令

| 命令 | 作用 |
|---|---|
| `scan` | 试运行：列出待转文件与各自通道，**不写任何文件** |
| `run` | 批量转换，中断后重跑自动续传 |
| `watch` | 常驻监控新增 / 修改的文件并自动转换 |
| `test <文件>` | 只转一个文件，看分流详情 |
| `convert <路径…>` | 只转点名的文件（也可给清单 `--list`，见使用说明） |
| `status` / `retry` / `ping` / `env` | 统计 / 重试失败 / 检测云端 OCR / 查看凭据生效情况 |

```bash
doc2md scan  --root ~/语料        # 试运行
doc2md run   --root ~/语料        # 正式转换
doc2md convert a.docx b.pdf       # 只转这一批
doc2md --help                     # 全部子命令与参数
```

公共参数：`--config <路径>`、`--root <目录>`（可重复）、`--limit N`、`--quiet`、
`--no-ocr`，写在子命令前后都认。Windows 上把 `doc2md` 换成 `doc2md.exe`；源码
运行时是 `python -m doc2md`。

## 文档

- **[详细使用说明](docs/USAGE.md)** —— 命令行全参数、图形界面、配置字段逐项说明、
  云端 OCR 多后端链路、安装与打包（含踩过的坑）、空文档与加密文件、注意事项。
- [规则档 PDF 引擎的上游说明](vendor/README.md) —— `vendor/ZhDocParser`。
