#!/usr/bin/env bash
# doc2md 一键安装（Linux / macOS）
#
#   一条命令（不用先下仓库）：
#   curl -fsSL https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh | bash
#
#   仓库内：
#   bash install.sh                    # 默认装到 ~/.local（有单文件二进制就用它，秒装）
#   bash install.sh --prefix /opt/doc2md
#   bash install.sh --source           # 强制源码安装（建 venv + pip 装依赖）
#   bash install.sh --bin /路径/doc2md # 指定现成的单文件可执行程序
#   bash install.sh --version v1.0.0   # 从 Release 拉指定版本（默认取最新版）
#   bash install.sh --uninstall        # 卸载（保留你的 config.json / .env / state.db 除非确认删除）
#
# 装完就有 `doc2md` 命令（放在 $PREFIX/bin）。程序把自己的 config.json /
# .env / state.db 写在 <前缀>/share/doc2md/ 里 —— 跟着程序走，不依赖当前目录。
#
# Windows 不用这个脚本：用打包好的 doc2md-安装程序.exe（见 README「安装」一节）。
set -euo pipefail

SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
else
    SCRIPT_DIR=""        # 管道模式（curl … | bash）：手上一个仓库文件都没有
fi

PREFIX="${DOC2MD_PREFIX:-$HOME/.local}"
BIN_ARG=""
VENV_ARG=""
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
FORCE_SOURCE=0
DO_UNINSTALL=0
ASSUME_YES=0
VERSION="${DOC2MD_VERSION:-}"
GH_PROXY="${DOC2MD_GH_PROXY:-}"
REPO="${DOC2MD_REPO:-https://github.com/jessehoo89/doc2md}"
SRC_DIR="${DOC2MD_SRC_DIR:-}"
STAGE_DIR=""

usage() {
    if [ -n "$SELF" ] && [ -f "$SELF" ]; then
        sed -n '2,18p' "$SELF" | sed 's/^# \{0,1\}//'
    else
        cat <<'USAGE'
doc2md 一键安装（Linux / macOS）

  一条命令（不用先下仓库）：
  curl -fsSL https://raw.githubusercontent.com/jessehoo89/doc2md/main/install.sh | bash

  常用参数：
  --prefix DIR      装到哪（默认 ~/.local）
  --version vX.Y.Z  从 Release 拉指定版本（默认取最新版）
  --source          强制源码安装（git clone + venv + pip，需 Python 3.11+）
  --bin FILE        用你手上现成的单文件可执行程序
  --gh-proxy URL    GitHub 加速前缀（如 https://gh-proxy.com/）
  --uninstall       卸载
  -y, --yes         卸载时不再确认

  环境变量：DOC2MD_PREFIX / DOC2MD_VERSION / DOC2MD_GH_PROXY / DOC2MD_REPO / DOC2MD_SRC_DIR
USAGE
    fi
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)    PREFIX="${2:?--prefix 后面要给目录}"; shift 2 ;;
        --bin)       BIN_ARG="${2:?--bin 后面要给文件}"; shift 2 ;;
        --venv)      VENV_ARG="${2:?--venv 后面要给目录}"; shift 2 ;;
        --mirror)    MIRROR="${2:?--mirror 后面要给 URL}"; shift 2 ;;
        --version)   VERSION="${2:?--version 后面要给版本号，如 v1.0.0}"; shift 2 ;;
        --gh-proxy)  GH_PROXY="${2:?--gh-proxy 后面要给前缀，如 https://gh-proxy.com/}"; shift 2 ;;
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

looks_like_binary() {
    [ -f "$1" ] && [ ! -d "$1" ] || return 1
    if command -v file >/dev/null 2>&1; then
        file -b "$1" | grep -qiE 'ELF|Mach-O' && return 0 || return 1
    fi
    head -c 4 "$1" | od -An -tx1 | grep -qi '7f 45 4c 46'   # ELF 魔数
}

# ---------------------------------------------------------------- 卸载
if [ "$DO_UNINSTALL" = 1 ]; then
    say "将要删除："
    say "  $LAUNCHER"
    [ -d "$LIBDIR" ] && say "  $LIBDIR/   （含 config.json / .env / state.db —— 删掉后需从头重转）"
    [ -d "$PREFIX/share/doc2md-src" ] && say "  $PREFIX/share/doc2md-src/   （源码方式克隆下来的仓库 + 它的 venv）"
    if [ "$ASSUME_YES" != 1 ]; then
        printf '确认删除？[y/N] '
        if [ -t 0 ]; then
            read -r ans
        elif [ -r /dev/tty ]; then
            read -r ans < /dev/tty      # 管道模式：stdin 是脚本本身，只能问终端
        else
            die "当前没法交互确认，请加 -y"
        fi
        case "$ans" in y|Y|yes|YES) ;; *) say "已取消。"; exit 0 ;; esac
    fi
    rm -f "$LAUNCHER"
    rm -rf "$LIBDIR"
    rm -rf "$PREFIX/share/doc2md-src"     # 源码方式克隆的仓库（连带它的 venv）
    rmdir "$BINDIR" "$PREFIX/share" 2>/dev/null || true   # 只剩空壳就顺手收掉
    say "已卸载：$LAUNCHER、$LIBDIR/ 与源码克隆目录均已删除。"
    exit 0
fi

# ---------------------------------------------------------------- 联网取文件
fetch() {                       # fetch <URL或本地路径> <输出文件>，失败返回非 0
    local url="$1" out="$2"
    if [ -f "$url" ]; then cp -f "$url" "$out"; return 0; fi
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o "$out" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 --timeout=20 -O "$out" "$url"
    else
        die "既没有 curl 也没有 wget，装一个再来：sudo apt install -y curl"
    fi
}

fetch_text() {                  # 取一段小文本到 stdout；失败给空串，不炸
    local url="$1"
    if [ -f "$url" ]; then cat "$url"; return 0; fi
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 20 --max-time 90 "$url" 2>/dev/null || true
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- --timeout=30 "$url" 2>/dev/null || true
    fi
}

ghurl() {                       # 需要时给 GitHub 链接套加速前缀
    if [ -n "$GH_PROXY" ]; then printf '%s%s' "$GH_PROXY" "$1"; else printf '%s' "$1"; fi
}

# ---------------------------------------------------------------- 自举（管道模式）
# curl … | bash 时没有仓库文件：先把「程序 + 文档模板」弄到临时目录，再走原来的安装流程。
# 二进制优先（秒装、不碰 Python）；下载不动且装了 git，就退回源码安装。
SLUG=""
case "$REPO" in
    https://github.com/*) SLUG="$(printf '%s' "${REPO#https://github.com/}" | sed 's#/*$##')" ;;
    */*)                  SLUG="$(printf '%s' "$REPO" | sed 's#/*$##')" ;;
esac
RAW_BASE="${DOC2MD_RAW:-https://raw.githubusercontent.com/${SLUG}/main}"
ASSET_BASE="${DOC2MD_ASSET_BASE:-$REPO/releases/download}"
API_URL="${DOC2MD_API:-https://api.github.com/repos/${SLUG}/releases/latest}"

check_version_format() {
    [ -z "$1" ] && return 0        # 没指定版本 = 用默认分支 / 最新版，合法
    case "$1" in
        *[!A-Za-z0-9._-]*) die "版本号格式不对：$1" ;;
    esac
}

is_github_repo() {
    case "$REPO" in https://github.com/*) return 0 ;; *) return 1 ;; esac
}

resolve_version() {             # 0 = 拿到了版本号；1 = 拿不到（调用方自己决定回退）
    [ -n "$VERSION" ] && return 0
    is_github_repo || return 1      # 非 GitHub 仓库就别去问 api.github.com 了
    # ① GitHub API（先直连；直连不通再走加速前缀，api.github.com 国内通常还能用）
    VERSION="$(fetch_text "$API_URL" \
        | sed -n 's/.*"tag_name" *: *"\([^"]*\)".*/\1/p' | head -1)"
    if [ -z "$VERSION" ]; then
        VERSION="$(fetch_text "$(ghurl "$API_URL")" \
            | sed -n 's/.*"tag_name" *: *"\([^"]*\)".*/\1/p' | head -1)"
    fi
    # ② 退回：看 /releases/latest 跳去哪
    if [ -z "$VERSION" ] && command -v curl >/dev/null 2>&1; then
        VERSION="$(curl -fsSI --connect-timeout 20 "$(ghurl "$REPO/releases/latest")" 2>/dev/null \
            | tr -d '\r' | sed -n 's#^[Ll]ocation: .*/tag/##p' | head -1)"
    fi
    # ③ 再退回：从页面里捞 /tag/vX.Y.Z
    if [ -z "$VERSION" ]; then
        VERSION="$(fetch_text "$(ghurl "$REPO/releases/latest")" \
            | grep -o '/tag/v[0-9][0-9A-Za-z._-]*' | head -1 | sed 's#/tag/##')"
    fi
    [ -n "$VERSION" ] || return 1
}

asset_name() {
    local os arch
    os="$(uname -s)"; arch="$(uname -m)"
    [ "$os" = "Linux" ] || die "官方二进制目前只有 Linux x86_64。macOS 请加 --source 从源码装（需 Python 3.11+）"
    case "$arch" in
        x86_64|amd64) ;;
        *) die "官方二进制只有 x86_64（当前 $arch）。请加 --source 从源码装" ;;
    esac
    printf 'doc2md-%s-linux-x86_64' "$1"
}

verify_sha256() {               # verify_sha256 <校验和文件> <文件名>（在文件所在目录里执行）
    local sums="$1" name="$2" line
    [ -s "$sums" ] || { note "没拿到校验和文件，跳过校验"; return 0; }
    line="$(awk -v n="$name" '$2 == n { print }' "$sums" | head -1)"
    [ -n "$line" ] || { note "校验和文件里没有 $name，跳过校验"; return 0; }
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s\n' "$line" | sha256sum -c - >/dev/null 2>&1 || return 1
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s\n' "$line" | shasum -a 256 -c - >/dev/null 2>&1 || return 1
    else
        note "系统没有 sha256sum/shasum，跳过校验"
        return 0
    fi
    say "  校验和 OK"
}

stage_docs() {                  # 抓文档与配置模板（best-effort，抓不到不影响安装）
    if [ -z "$STAGE_DIR" ]; then
        STAGE_DIR="$(mktemp -d)"
        trap 'rm -rf "$STAGE_DIR"' EXIT
    fi
    [ -n "$SCRIPT_DIR" ] || SCRIPT_DIR="$STAGE_DIR"   # 让后面的安装步骤找得到这些文件
    local f
    for f in README.md LICENSE config.example.json .env.example; do
        fetch_text "$(ghurl "$RAW_BASE/$f")" > "$STAGE_DIR/$f" || true
        [ -s "$STAGE_DIR/$f" ] || rm -f "$STAGE_DIR/$f"
    done
    mkdir -p "$STAGE_DIR/docs"
    fetch_text "$(ghurl "$RAW_BASE/docs/USAGE.md")" > "$STAGE_DIR/docs/USAGE.md" || true
    [ -s "$STAGE_DIR/docs/USAGE.md" ] || rm -f "$STAGE_DIR/docs/USAGE.md"
}

download_binary() {             # 成功则设好 BIN_ARG / SCRIPT_DIR，返回 0；失败返回 1
    resolve_version || { note "拿不到版本号（网络或仓库问题）"; return 1; }
    check_version_format "$VERSION"
    local asset url
    asset="$(asset_name "$VERSION")"
    STAGE_DIR="$(mktemp -d)"
    trap 'rm -rf "$STAGE_DIR"' EXIT
    url="$(ghurl "$ASSET_BASE/$VERSION/$asset")"
    say "== 下载 $VERSION 的现成程序（约 130MB，慢的话耐心等）=="
    say "   $url"
    if ! fetch "$url" "$STAGE_DIR/$asset"; then
        note "程序没下下来。国内直连 GitHub 经常慢或连不上，两条路："
        note "  ① 套加速前缀重跑：加 --gh-proxy https://gh-proxy.com/（或 https://ghproxy.net/）"
        note "  ② 自己下好 Release 里的 $asset，再用：--bin <那个文件>"
        return 1
    fi
    looks_like_binary "$STAGE_DIR/$asset" || { note "下回来的不是可执行文件"; return 1; }
    ( cd "$STAGE_DIR" && fetch_text "$(ghurl "$ASSET_BASE/$VERSION/SHA256SUMS-linux.txt")" > SHA256SUMS-linux.txt ) || true
    # 校验不过 = 文件坏了，直接停下（别再花时间走源码安装）
    ( cd "$STAGE_DIR" && verify_sha256 SHA256SUMS-linux.txt "$asset" ) \
        || die "下载的文件校验和不通过，重跑一次；或加 --gh-proxy 换个通道、用 --bin 指本地文件"
    say "  程序  → 下载完成（$(du -h "$STAGE_DIR/$asset" | cut -f1)）"
    BIN_ARG="$STAGE_DIR/$asset"
    SCRIPT_DIR="$STAGE_DIR"
    return 0
}

install_from_git() {            # 源码模式：克隆仓库（留在前缀里，启动器要指过去）
    command -v git >/dev/null 2>&1 || die "现成程序下载不动，又没有 git 可用。请手动下 Release 里的文件，或先装 git"
    check_version_format "$VERSION"
    local dir="${SRC_DIR:-$PREFIX/share/doc2md-src}"
    say "== 改为源码安装：仓库克隆到 $dir =="
    if [ -d "$dir/.git" ]; then
        ( cd "$dir" && git pull --ff-only ) || note "更新失败，用现有代码继续"
    else
        mkdir -p "$(dirname "$dir")"
        if [ -n "$VERSION" ]; then
            git clone --depth 1 --branch "$VERSION" "$(ghurl "$REPO")" "$dir" || clone_failed
        else
            git clone --depth 1 "$(ghurl "$REPO")" "$dir" || clone_failed
        fi
    fi
    SCRIPT_DIR="$dir"
    FORCE_SOURCE=1
    [ -n "$VENV_ARG" ] || VENV_ARG="$dir/.venv"
}

clone_failed() {
    die "克隆失败。国内网络可试加 --gh-proxy https://gh-proxy.com/；或手动下好 Release 里的程序后用 --bin <文件>"
}

if [ -z "$SCRIPT_DIR" ]; then
    if [ "$FORCE_SOURCE" = 1 ]; then
        install_from_git
    elif [ -n "$BIN_ARG" ]; then
        stage_docs                       # 自带程序文件：只补文档
    elif ! download_binary; then
        note "改用源码安装"
        install_from_git
    else
        stage_docs
    fi
fi

# ---------------------------------------------------------------- 找单文件二进制
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
VER_OUT=""
if VER_OUT="$("$LAUNCHER" --version 2>&1)"; then
    printf '%s\n' "$VER_OUT" | head -2
else
    printf '%s\n' "$VER_OUT" | head -5 >&2
    # 现成程序跑不起来：多半是打包机的 glibc 比这台新（报 GLIBC_2.xx not found）。
    # 本机有 git + Python 3.11+ 就自动改源码安装，别让用户自己琢磨。
    if [ -n "$SRC_BIN" ] && [ "$FORCE_SOURCE" != 1 ] \
       && command -v git >/dev/null 2>&1 && pick_python >/dev/null 2>&1; then
        note "现成程序在这台机器上跑不起来（常见原因：打包机 glibc 比本机新）。"
        note "  自动改用源码安装重来一遍……"
        rm -f "$LAUNCHER"; rm -rf "$LIBDIR"
        install_from_git
        install_source
        PROGRAM_DIR="$SCRIPT_DIR"
        if [ -f "$SCRIPT_DIR/.env.example" ] && [ ! -f "$PROGRAM_DIR/.env" ]; then
            install -m 600 "$SCRIPT_DIR/.env.example" "$PROGRAM_DIR/.env"
            say "  凭据模板 → $PROGRAM_DIR/.env（值留空）"
        fi
        say ""
        VER_OUT="$("$LAUNCHER" --version 2>&1)" \
            || { printf '%s\n' "$VER_OUT" | head -5 >&2; die "源码方式也没跑起来，把上面的报错发出来看看"; }
        printf '%s\n' "$VER_OUT" | head -2
    else
        die "装好了但跑不起来（上面是报错）。两条路：① 本机装 git 与 Python 3.11+ 后加 --source 重跑；② 直接在 Windows 上用安装程序"
    fi
fi
say ""
say "装好了。用法："
say "  doc2md                      # 中文菜单（不带参数）"
say "  doc2md scan   --root 语料目录   # 试运行，不写文件"
say "  doc2md run    --root 语料目录   # 正式转换，中断可续传"
say "  doc2md --help               # 全部子命令"
say "配置与状态库在：$PROGRAM_DIR/"
say "详细说明：$PROGRAM_DIR/docs/USAGE.md"