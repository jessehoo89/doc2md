# doc2md · 文档批量转 Markdown

把一整个目录树的 **docx / doc / xls / xlsx / pdf** 批量转成 Markdown，并带上
**断点续传**、**实时监控**、**扫描件 OCR（多云端后端自动熔断切换）** 三件事。

为中文公文归档场景做的：段落重组、页码过滤、标题识别、落款分行、表格还原、
敏感目录不上云。

```
┌── 本地直转（不联网、最快） ────────────────────────────────┐
│  .docx  → mammoth        .xlsx → openpyxl                 │
│  .doc   → Office COM     .xls  → Office COM               │
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

---

## 快速开始

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

`_python.bat` 是给上面几个 bat 共用的解释器探测脚本，**不要单独双击**。
查找顺序：`%DOC2MD_PYTHON%` → `.venv\Scripts\python.exe` → `venv\Scripts\python.exe`
→ PATH 上第一个真能跑的 `python`（微软商店那个占位程序会被识别并跳过）。

---

## 目录结构

```
doc2md/
├── doc2md/                 # 主程序包
│   ├── cli.py              #   命令行入口与各子命令
│   ├── engine.py           #   调度核心：计划、路由、断点续传、md 落盘
│   ├── converters.py       #   本地格式转换 + Markdown 清洗与段落重组
│   ├── detect.py           #   真实格式嗅探 + PDF 文本层可信度判定
│   ├── ocr_router.py       #   多后端路由器：优先级链路 + 熔断切换
│   ├── ocr.py              #   PaddleOCR 客户端 + 错误分类
│   ├── mineru.py           #   MinerU 客户端（precision / agent 两种模式）
│   ├── vlm.py              #   通用 OpenAI 兼容视觉模型客户端（接专用 OCR 模型）
│   ├── local_ocr.py        #   本地 RapidOCR 子进程客户端（可选）
│   ├── com.py              #   Office COM 转换（.doc/.xls）
│   ├── watcher.py          #   实时监控模式
│   ├── config.py           #   配置加载 + .env 凭据注入
│   └── state.py            #   SQLite 状态库（断点续传的依据）
├── tests/                  # 回归 / 集成测试
├── scripts/                # 运维脚本（清理、体检、隔离，默认 dry-run）
├── devkit.py               # 开发辅助：从 config.json 的 roots 自动挑样例
├── launcher.py             # 菜单式启动器（被 文档转MD.bat 调用）
├── config.example.json     # 配置模板（提交进仓库；真正的 config.json 被忽略）
├── .env.example            # 凭据模板（同上）
└── requirements.txt
```

---

## 命令行

| 命令 | 作用 |
|---|---|
| `python -m doc2md scan` | 试运行：列出待转文件与各自通道，**不写任何文件** |
| `python -m doc2md run` | 批量转换，中断后重跑自动续传 |
| `python -m doc2md watch` | 常驻监控新增/修改的文件并自动转换 |
| `python -m doc2md test <文件>` | 只转一个文件 |
| `python -m doc2md status` | 统计（含各后端今日用量） |
| `python -m doc2md retry` | 重试失败的文件（`--clear` 仅清记录） |
| `python -m doc2md ping` | 检测各云端 OCR 后端的就绪与连通性 |
| `python -m doc2md env` | 查看 `.env` 与各后端 Token 的生效情况 |

公共参数：`--config <路径>`、`--root <目录>`（可重复，覆盖配置）、
`--limit N`、`--quiet`、`--no-ocr`。写在子命令前后都认。

---

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
| `local_ocr.python_exe` | 本地 RapidOCR 环境；**留空即自动禁用本地 OCR** |

### 关于 `on_collision`（同名冲突）

`x.docx` 与 `x.pdf` 是同一份公文的两种格式时会算出同一个 `x.md`。

- **`stable`（默认）** —— 先到先得 + 归属粘住：谁先拿到哪个名字就永久不变，
  重转只覆盖自己那一份，**绝不改名、绝不多冒第三个文件**，内容一个字不丢。
- `overwrite` —— 一个名字只剩一个文件。代价：同一文档两种格式内容不同时会丢一半。
- `suffix` —— 旧行为，每次重转重新抢，名字会漂移。**不建议**。

---

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

---

## 开发与测试

```bat
:: 策略测试：151 项断言，假后端，不联网、不耗配额
.venv\Scripts\python.exe tests\test_ocr_router.py

:: 输出命名策略端到端（需要一个真实 docx + xlsx 做样本）
.venv\Scripts\python.exe tests\test_output_naming.py

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

---

## 注意事项

- **改了代码对已在运行的进程不生效** —— 监控模式要重启才是新逻辑。
- 新代码对已在运行的进程不生效；`config.json` 的 `output` 段支持热加载
  （约 2 秒生效），`ocr` 段的改动需要重启。
- `state.db` 是断点续传的唯一依据，**不要提交、也不要随意删除**；想重转某批
  文件，从状态库里删掉对应记录即可（`scripts/` 里有现成的工具）。
- `.doc` / `.xls` 走 Office COM，需要本机装了 Office；没有时会走降级路径。
- 本地 OCR 需要**另一个**装了 `rapidocr` + `onnxruntime` 的 Python 环境，
  在 `local_ocr.python_exe` 里指过去；不装也不影响，云端链路能覆盖。

---

## 许可

内部工具，未附许可协议。
