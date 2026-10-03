doc2md：把文档批量转成 Markdown 的命令行工具。

支持 `.docx` / `.xlsx` / `.doc` / `.xls` / `.pdf`（含扫描件 OCR）批量转换，可递归整个目录。

### 下载哪个文件

| 文件 | 适用 | 说明 |
|:--|:--|:--|
| `*-linux-x86_64` | Linux | 单个可执行文件。下载后 `chmod +x` 再加执行位即可运行；带云端 OCR 时在其旁放一份 `.env`，不上云也能用本地通道 |
| `*-linux-x86_64-onedir.zip` | Linux | 多文件版，解压即用。启动略快，适合固定放在服务器上 |
| `*-win-x64-installer.exe` | Windows | 单文件安装程序，双击安装（自带程序与文档，离线机可用） |
| `*-win-x64.zip` | Windows | 多文件免安装版，解压后运行 `doc2md.exe`。⚠️ 整个文件夹要一起保留，单独拷 exe 跑不起来 |

### 用法

```
# Linux 单文件版
chmod +x doc2md-*-linux-x86_64
./doc2md-*-linux-x86_64 convert <文件或目录>

# Windows 免安装版
doc2md.exe convert <文件或目录>
```

不带参数直接运行会进入中文交互菜单。详细说明见仓库 `docs/USAGE.md`（Windows 安装包里也带了一份）。

### 环境要求

- Linux：单文件版与 onedir 版按 glibc 2.36 链接（在 Debian 12 上打包），Debian 12 / Ubuntu 23.04 及更新的系统直接可用；更旧的系统会报 `GLIBC_2.xx not found`，请用仓库里的 `install.sh --source` 源码安装（需要 Python 3.11+）
- Linux：老式 `.doc` / `.xls` 需要系统装了 LibreOffice（`sudo apt install libreoffice-writer libreoffice-calc`）；缺了只影响这两种格式，其余照常
- Windows：老式 `.doc` / `.xls` 走 Office COM，需装 Office
- 扫描件 PDF 转文字走云端 OCR，需要在 `.env` 填入 token；不填则自动跳过 OCR，其余格式不受影响

⚠️ 国内直连 GitHub 下载可能偏慢，如遇超时请换网络或使用代理重试。