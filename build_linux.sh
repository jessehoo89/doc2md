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
    echo "  类型：$(file -b "$BIN" | cut -c1-55)"
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
    echo "  类型：$(file -b "$BIN" | cut -c1-55)"
    # 冷启动（首次要解包）与热启动各测一次
    local t1 t2
    t1=$( { /usr/bin/time -f %e "$BIN" --version >/dev/null; } 2>&1 | tail -1 )
    t2=$( { /usr/bin/time -f %e "$BIN" --version >/dev/null; } 2>&1 | tail -1 )
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