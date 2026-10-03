#!/usr/bin/env bash
# 在 Linux 上打包 doc2md（PyInstaller）
#
#   bash build_linux.sh            # onedir（默认）：dist/doc2md/ 目录，启动最快
#   bash build_linux.sh onefile    # 单文件：dist-onefile/doc2md 一个文件，便于分发
#   bash build_linux.sh both       # 两种都打
#
# 环境变量：
#   DOC2MD_VENV   指定虚拟环境目录（默认 .venv，其次 .venv-gui）
#
# 说明：
#   - onedir  出 dist/doc2md/{doc2md,_internal/}；解释器有 tkinter 时多一个
#     doc2md-gui（窗口版）。整个目录要一起拷走，依赖在 _internal 里。
#   - onefile 出 dist-onefile/doc2md 单个可执行文件，启动时自解压到临时目录
#     （_MEIPASS，退出即删），所以启动比 onedir 慢若干秒、占 /tmp 空间；
#     只出控制台版。
#   - 两种形态都把 config.json / .env / state.db 放在程序文件旁边（按
#     sys.executable 解析，不是解包目录），配置跟着程序走。
#   - .doc / .xls 转换仍依赖本机 LibreOffice（soffice），打不进二进制；
#     本地 RapidOCR 同样需要独立 Python 环境——属能力边界。
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-onedir}"
case "$MODE" in
    onedir|onefile|both) ;;
    *) echo "用法：bash build_linux.sh [onedir|onefile|both]"; exit 2 ;;
esac

# ---- 挑解释器：优先显式指定，其次 .venv，再 .venv-gui ----
PY=""
for cand in "${DOC2MD_VENV:-}" .venv .venv-gui; do
    [ -n "$cand" ] || continue
    if [ -x "$cand/bin/python" ]; then PY="$cand/bin/python"; break; fi
done
if [ -z "$PY" ]; then
    echo "没找到虚拟环境。先建一个再装依赖："
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
echo "使用解释器：$PY ($("$PY" -c 'import sys;print(sys.version.split()[0])'))  形态：$MODE"

# ---- 展示用的小工具：有就用、没有就退化 —— 这些不是构建的必需环节，不能拖垮整个打包 ----
bin_type() {                    # 识别产物类型；本机没装 file 就退化成 ELF 魔数
    if command -v file >/dev/null 2>&1; then
        file -b "$1" 2>/dev/null | cut -c1-55
    else
        head -c 4 "$1" | od -An -tx1 | tr -d ' \n'      # 7f454c46 = ELF
    fi
}
elapsed() {                     # 测启动耗时；不依赖 /usr/bin/time（精简系统常没有）
    local s e rc
    s=$(date +%s%N); "$@" >/dev/null 2>&1; rc=$?; e=$(date +%s%N)
    awk -v a="$s" -v b="$e" 'BEGIN { printf "%.2f", (b-a)/1000000000 }'
    return $rc
}

# ---- 打包环境守卫：产物要求多新的 glibc，取决于构建机的 glibc ----
# 2026-10-03 教训：在 ubuntu-latest（glibc 2.39）上打包，产物要求 GLIBC_2.38，
# Debian 12（2.36）用户装完直接跑不起来（且下载校验和完全正确，很难往这里想）。
MAX_GLIBC="${DOC2MD_MAX_GLIBC:-2.36}"        # 目标底线：Debian 12 / Ubuntu 23.04
# ⚠️ 取版本号别用 "ldd --version | head -1"：head 提前退出会让 ldd 吃 SIGPIPE，
#    set -o pipefail 下整条管道变 141，赋值失败 → set -e 直接退出（2026-10-03 CI 就这么挂的）。
#    getconf 只输出一行，配合 || true 双保险；取不到再退回 sed -n '1p'（sed 会读完输入，不关管道）。
BUILD_GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | grep -oE '[0-9]+\.[0-9]+$' || true)"
[ -n "$BUILD_GLIBC" ] || BUILD_GLIBC="$(ldd --version 2>/dev/null | sed -n '1p' | grep -oE '[0-9]+\.[0-9]+$' || true)"
if [ -n "$BUILD_GLIBC" ]; then
    if [ "$(printf '%s\n%s\n' "$MAX_GLIBC" "$BUILD_GLIBC" | sort -V | tail -1)" = "$MAX_GLIBC" ]; then
        echo "构建机 glibc $BUILD_GLIBC ≤ 目标 $MAX_GLIBC ✔"
    else
        echo "⚠️ 构建机 glibc 是 $BUILD_GLIBC，比目标 $MAX_GLIBC 新：产物在 Debian 12 一类系统上会报"
        echo "   「GLIBC_$BUILD_GLIBC not found」。请在更旧的容器里打包（CI 用 debian:12）；"
        echo "   确实要在新系统上打包就设 DOC2MD_ALLOW_NEW_GLIBC=1 跳过。"
        [ "${DOC2MD_ALLOW_NEW_GLIBC:-}" = 1 ] || exit 1
    fi
fi

# ---- 依赖自检 ----
"$PY" -c "import pymupdf, pymupdf4llm, mammoth, openpyxl" 2>/dev/null \
    || { echo "缺依赖，先跑：$PY -m pip install -r requirements.txt"; exit 1; }
"$PY" -m PyInstaller --version >/dev/null 2>&1 \
    || { echo "装 PyInstaller…"; "$PY" -m pip install pyinstaller; }

# ---- soffice（老式 doc/xls 必需）----
if ! command -v soffice >/dev/null 2>&1 && ! command -v libreoffice >/dev/null 2>&1; then
    echo "[警告] 本机没有 soffice/libreoffice，打出来的程序转不了 .doc/.xls"
    echo "       （sudo apt install libreoffice-core libreoffice-writer libreoffice-calc）"
fi

build_onedir() {
    echo
    echo "== 打包 onedir（exe + _internal/）=="
    "$PY" -m PyInstaller doc2md.spec --noconfirm --distpath dist --workpath build
    local BIN=dist/doc2md/doc2md
    [ -x "$BIN" ] || { echo "失败：没看到 $BIN"; return 1; }
    echo "  产物：$(pwd)/dist/doc2md/"
    echo "  体积：$(du -sh dist/doc2md | cut -f1)  文件数：$(find dist/doc2md | wc -l)"
    echo "  类型：$(bin_type "$BIN")"
    ls dist/doc2md | grep -v _internal | sed 's/^/    /'
}

build_onefile() {
    echo
    echo "== 打包 onefile（单个可执行文件）=="
    DOC2MD_ONEFILE=1 "$PY" -m PyInstaller doc2md.spec --noconfirm \
        --distpath dist-onefile --workpath build-onefile
    local BIN=dist-onefile/doc2md
    [ -x "$BIN" ] || { echo "失败：没看到 $BIN"; return 1; }
    echo "  产物：$(pwd)/$BIN"
    echo "  体积：$(du -sh "$BIN" | cut -f1)   （onedir 目录打包前的压缩档，故小于目录体积）"
    echo "  类型：$(bin_type "$BIN")"
    # 冷启动（首次要解包）与热启动各测一次；跑不起来就直接判构建失败（别把坏产物推给用户）
    local t1 t2
    t1=$(elapsed "$BIN" --version) || { echo "失败：单文件版打出来却跑不起来，报错如下"; "$BIN" --version; return 1; }
    t2=$(elapsed "$BIN" --version) || true
    echo "  启动耗时：冷 ${t1}s / 热 ${t2}s（每次启动都要解包到临时目录）"
    echo "  解包位置：\${TMPDIR:-/tmp}/_MEIxxxxxx（进程退出即删）"
    echo "  单文件版随附配置：把 .env 放在可执行文件旁边才生效"
}

case "$MODE" in
    onedir)  build_onedir ;;
    onefile) build_onefile ;;
    both)    build_onedir; build_onefile ;;
esac

echo
echo "运行方式："
[ "$MODE" != "onefile" ] && echo "  ./dist/doc2md/doc2md            # 不带参数 = 中文菜单"
[ "$MODE" != "onedir" ]  && echo "  ./dist-onefile/doc2md           # 单文件版，整包分发只需这一个文件"
echo "  <程序> convert <路径> --quiet   # 等价于 python -m doc2md convert <路径>"