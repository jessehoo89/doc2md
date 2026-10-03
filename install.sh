#!/usr/bin/env bash
# doc2md 一键安装（Linux / macOS）
#
#   bash install.sh                    # 默认装到 ~/.local（有单文件二进制就用它，秒装）
#   bash install.sh --prefix /opt/doc2md
#   bash install.sh --source           # 强制源码安装（建 venv + pip 装依赖）
#   bash install.sh --bin /路径/doc2md # 指定现成的单文件可执行程序
#   bash install.sh --uninstall        # 卸载（保留你的 config.json / .env / state.db 除非确认删除）
#
# 装完就有 `doc2md` 命令（放在 $PREFIX/bin）。程序把自己的 config.json /
# .env / state.db 写在 <前缀>/share/doc2md/ 里 —— 跟着程序走，不依赖当前目录。
#
# Windows 不用这个脚本：用打包好的 doc2md-安装程序.exe（见 README「安装」一节）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PREFIX="${DOC2MD_PREFIX:-$HOME/.local}"
BIN_ARG=""
VENV_ARG=""
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
FORCE_SOURCE=0
DO_UNINSTALL=0
ASSUME_YES=0

usage() {
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)    PREFIX="${2:?--prefix 后面要给目录}"; shift 2 ;;
        --bin)       BIN_ARG="${2:?--bin 后面要给文件}"; shift 2 ;;
        --venv)      VENV_ARG="${2:?--venv 后面要给目录}"; shift 2 ;;
        --mirror)    MIRROR="${2:?--mirror 后面要给 URL}"; shift 2 ;;
        --source)    FORCE_SOURCE=1; shift ;;
        --uninstall) DO_UNINSTALL=1; shift ;;
        -y|--yes)    ASSUME_YES=1; shift ;;
        -h|--help)   usage 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）"; exit 2 ;;
    esac
done

LIBDIR="$PREFIX/share/doc2md"
BINDIR="$PREFIX/bin"
LAUNCHER="$BINDIR/doc2md"

say()  { printf '%s\n' "$*"; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }
note() { printf '[提示] %s\n' "$*"; }

# ---------------------------------------------------------------- 卸载
if [ "$DO_UNINSTALL" = 1 ]; then
    say "将要删除："
    say "  $LAUNCHER"
    [ -d "$LIBDIR" ] && say "  $LIBDIR/   （含 config.json / .env / state.db —— 删掉后需从头重转）"
    if [ "$ASSUME_YES" != 1 ]; then
        printf '确认删除？[y/N] '
        read -r ans
        case "$ans" in y|Y|yes|YES) ;; *) say "已取消。"; exit 0 ;; esac
    fi
    rm -f "$LAUNCHER"
    rm -rf "$LIBDIR"
    say "已卸载。$PREFIX/bin 下的 $LAUNCHER 与 $LIBDIR/ 均已删除。"
    exit 0
fi

# ---------------------------------------------------------------- 找单文件二进制
looks_like_binary() {
    [ -f "$1" ] && [ ! -d "$1" ] || return 1
    if command -v file >/dev/null 2>&1; then
        file -b "$1" | grep -qiE 'ELF|Mach-O' && return 0 || return 1
    fi
    head -c 4 "$1" | od -An -tx1 | grep -qi '7f 45 4c 46'   # ELF 魔数
}

SRC_BIN=""
for cand in "$BIN_ARG" "${DOC2MD_BIN:-}" "$SCRIPT_DIR/dist-onefile/doc2md" "$SCRIPT_DIR/doc2md"; do
    [ -n "$cand" ] || continue
    if looks_like_binary "$cand"; then SRC_BIN="$cand"; break; fi
done
[ -n "$BIN_ARG" ] && [ -z "$SRC_BIN" ] && die "--bin 指定的不是可执行程序：$BIN_ARG"

if [ "$FORCE_SOURCE" = 1 ]; then SRC_BIN=""; fi

# ---------------------------------------------------------------- 安装（二进制）
install_binary() {
    say "== 安装方式：单文件可执行程序（无需 Python）=="
    mkdir -p "$LIBDIR" "$BINDIR"
    install -m 755 "$SRC_BIN" "$LIBDIR/doc2md"
    ln -sf "$LIBDIR/doc2md" "$LAUNCHER"
    say "  程序  → $LIBDIR/doc2md  ($(du -h "$LIBDIR/doc2md" | cut -f1))"
    say "  命令  → $LAUNCHER"
    # 文档、许可放程序旁边：离线机器上也查得到（README 的相对链接指向 docs/USAGE.md）
    for f in README.md LICENSE; do
        if [ -f "$SCRIPT_DIR/$f" ]; then
            install -m 644 "$SCRIPT_DIR/$f" "$LIBDIR/$f"
        fi
    done
    if [ -f "$SCRIPT_DIR/docs/USAGE.md" ]; then
        mkdir -p "$LIBDIR/docs"
        install -m 644 "$SCRIPT_DIR/docs/USAGE.md" "$LIBDIR/docs/USAGE.md"
        say "  文档  → $LIBDIR/docs/USAGE.md"
    fi
}

# ---------------------------------------------------------------- 安装（源码）
pick_python() {
    local p
    for p in python3.12 python3.11 python3; do
        command -v "$p" >/dev/null 2>&1 || continue
        "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null && { echo "$p"; return 0; }
    done
    return 1
}

install_source() {
    [ -f "$SCRIPT_DIR/requirements.txt" ] && [ -d "$SCRIPT_DIR/doc2md" ] \
        || die "源码安装要在仓库根运行本脚本（找不到 requirements.txt 与 doc2md/ 目录）"
    say "== 安装方式：源码 + 虚拟环境 =="

    local VENV="${VENV_ARG:-$SCRIPT_DIR/.venv}"
    if [ -x "$VENV/bin/python" ]; then
        say "  复用已有虚拟环境：$VENV"
    else
        local PY
        PY="$(pick_python)" || die "找不到 Python 3.11+。装一个再回来：sudo apt install -y python3 python3-venv"
        say "  建虚拟环境：$VENV（用 $("$PY" --version 2>&1)）"
        "$PY" -m venv "$VENV"
    fi

    local PYV="$VENV/bin/python"
    if "$PYV" -c 'import pymupdf, pymupdf4llm, mammoth, openpyxl' 2>/dev/null; then
        say "  依赖已就绪，跳过 pip 安装"
    else
        say "  安装依赖（首次约几十 MB，耐心等）…"
        "$PYV" -m pip install --upgrade pip >/dev/null 2>&1 || true
        if ! "$PYV" -m pip install -r "$SCRIPT_DIR/requirements.txt"; then
            note "直连 PyPI 失败，换镜像重试：$MIRROR"
            "$PYV" -m pip install -i "$MIRROR" -r "$SCRIPT_DIR/requirements.txt"
        fi
    fi

    mkdir -p "$BINDIR"
    cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# doc2md 启动器（install.sh 生成）—— 源码安装：<repo>/.venv
cd "$SCRIPT_DIR" && exec "$VENV/bin/python" -m doc2md "\$@"
EOF
    chmod 755 "$LAUNCHER"
    say "  源码  → $SCRIPT_DIR"
    say "  命令  → $LAUNCHER"
}

if [ -n "$SRC_BIN" ]; then
    install_binary
    PROGRAM_DIR="$LIBDIR"          # 二进制版：程序根 = 可执行文件所在目录
else
    install_source
    PROGRAM_DIR="$SCRIPT_DIR"      # 源码版：程序根 = 仓库根
fi

# ---------------------------------------------------------------- 凭据与配置模板
# 模板必须铺在「程序根」下，否则程序读不到（.env 按程序根查找）。
if [ -f "$SCRIPT_DIR/.env.example" ] && [ ! -f "$PROGRAM_DIR/.env" ]; then
    install -m 600 "$SCRIPT_DIR/.env.example" "$PROGRAM_DIR/.env"
    say "  凭据模板 → $PROGRAM_DIR/.env（值留空，填了才有云端 OCR）"
fi
if [ -f "$SCRIPT_DIR/config.example.json" ] && [ ! -f "$PROGRAM_DIR/config.example.json" ]; then
    install -m 644 "$SCRIPT_DIR/config.example.json" "$PROGRAM_DIR/config.example.json"
fi

# ---------------------------------------------------------------- 环境自检
say ""
say "== 自检 =="
if command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1; then
    say "  LibreOffice：有（.doc / .xls 可转）"
else
    note "  LibreOffice：没有 → .doc / .xls / .wps / .et 转不了（docx / xlsx / PDF 不受影响）"
    note "               Debian/Ubuntu 装法：sudo apt install -y libreoffice-writer libreoffice-calc"
fi
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) note "  $BINDIR 不在 PATH 里，现在这条命令加一下（写进 ~/.bashrc 可长期生效）："
       note "      export PATH=\"$BINDIR:\$PATH\"" ;;
esac

say ""
say "== 验证 =="
"$LAUNCHER" --version 2>&1 | head -2 || die "装好了但跑不起来，把上面的报错发出来看看"
say ""
say "装好了。用法："
say "  doc2md                      # 中文菜单（不带参数）"
say "  doc2md scan   --root 语料目录   # 试运行，不写文件"
say "  doc2md run    --root 语料目录   # 正式转换，中断可续传"
say "  doc2md --help               # 全部子命令"
say "配置与状态库在：$PROGRAM_DIR/"
say "详细说明：$PROGRAM_DIR/docs/USAGE.md"