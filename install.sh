#!/usr/bin/env bash
# ============================================================================
# Canvas Grader 环境安装脚本 (macOS / Linux)
#
# 必需:
#   - Python 3.9+ (带 tkinter)
#   - 虚拟环境 .venv + requests + playwright
#   - Google Chrome (浏览器登录用; 没有 Chrome 时安装 Playwright 自带的 Chromium)
# 可选 (AI 批改用, 会逐项询问):
#   - Java JDK       让 AI 编译运行学生的 Java 代码
#   - Claude Code    命令行版 (Claude 桌面版已自带, 通常不需要)
#   - Codex CLI
#
# 用法:
#   bash install.sh            交互安装 (可选组件逐项询问)
#   bash install.sh --all      安装全部可选组件, 不询问
#   bash install.sh --minimal  只装必需组件
#
# 安装完成后双击 "Canvas Grader (中文).command" 等启动器 (macOS), 或运行 ./run_grader.sh [en|ko]
# ============================================================================

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
APP="$DIR/canvas_grader_gui.py"
MODE="ask"
case "${1:-}" in
    --all) MODE="all" ;;
    --minimal) MODE="minimal" ;;
    "") ;;
    *) echo "用法: bash install.sh [--all | --minimal]"; exit 1 ;;
esac

OS="$(uname -s)"
bold()  { printf "\n\033[1m== %s ==\033[0m\n" "$*"; }
ok()    { printf "  \033[32m✔\033[0m %s\n" "$*"; }
warn()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
fail()  { printf "  \033[31m✘\033[0m %s\n" "$*"; exit 1; }
has()   { command -v "$1" >/dev/null 2>&1; }

ask() {   # ask "问题" → 0 = 是
    case "$MODE" in
        all) return 0 ;;
        minimal) return 1 ;;
    esac
    local reply
    read -r -p "  $1 [y/N] " reply </dev/tty || return 1
    [[ "$reply" =~ ^[Yy] ]]
}

[[ -f "$APP" ]] || fail "找不到 $APP — 请把 install.sh 放在 canvas_grader_gui.py 同一个文件夹里运行"

# Homebrew (macOS): 让新开的 shell 也能找到 brew
if [[ "$OS" == "Darwin" ]] && ! has brew; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [[ -x "$b" ]] && eval "$("$b" shellenv)" && break
    done
fi

# ----------------------------------------------------------------------------
bold "1. Python 3.9+ (带 tkinter)"
PY=""
python_ok() {
    "$1" - <<'EOF' >/dev/null 2>&1
import sys, tkinter, venv
assert sys.version_info >= (3, 9)
tkinter.Tcl()
EOF
}
for cand in python3.13 python3.12 python3.11 python3.10 python3 python3.9 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if has "$cand" && python_ok "$cand"; then PY="$(command -v "$cand")"; break; fi
done

if [[ -z "$PY" ]]; then
    warn "没有找到带 tkinter 的 Python 3.9+"
    if [[ "$OS" == "Darwin" ]] && has brew; then
        echo "  将用 Homebrew 安装: brew install python@3.12 python-tk@3.12"
        brew install python@3.12 python-tk@3.12
        PY="$(brew --prefix)/opt/python@3.12/bin/python3.12"
    elif [[ "$OS" == "Darwin" ]]; then
        fail "请先从 https://www.python.org/downloads/ 安装 Python 3 (自带 tkinter), 然后重新运行本脚本"
    else
        fail "请先安装 Python 3.9+ 和 tkinter, 例如: sudo apt install python3 python3-venv python3-tk"
    fi
    python_ok "$PY" || fail "$PY 仍然无法使用 tkinter"
fi
ok "$("$PY" --version)  ($PY)"

# ----------------------------------------------------------------------------
bold "2. 虚拟环境 .venv + requests + playwright"
if [[ -x "$VENV/bin/python" ]] && ! python_ok "$VENV/bin/python"; then
    warn "已有的 .venv 不可用, 重新创建"
    rm -rf "$VENV"
fi
[[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade requests playwright
ok "$("$VENV/bin/python" -c 'import requests, importlib.metadata as m; print("requests", requests.__version__, "/ playwright", m.version("playwright"))')"

# ----------------------------------------------------------------------------
bold "3. 浏览器 (用于登录 Canvas)"
CHROME=""
if [[ "$OS" == "Darwin" ]]; then
    [[ -d "/Applications/Google Chrome.app" || -d "$HOME/Applications/Google Chrome.app" ]] && CHROME=1
else
    { has google-chrome || has google-chrome-stable; } && CHROME=1
fi
if [[ -n "$CHROME" ]]; then
    ok "已安装 Google Chrome"
else
    warn "没有找到 Google Chrome, 安装 Playwright 自带的 Chromium (约 150 MB)"
    if [[ "$OS" == "Darwin" ]]; then
        "$VENV/bin/python" -m playwright install chromium
    else
        "$VENV/bin/python" -m playwright install --with-deps chromium
    fi
    ok "Chromium 已安装"
fi

# ----------------------------------------------------------------------------
bold "4. 可选: AI 批改用到的工具"

# Java JDK: macOS 上 /usr/bin/javac 只是占位程序, 用 java_home 判断
java_ok() {
    if [[ "$OS" == "Darwin" ]]; then
        /usr/libexec/java_home >/dev/null 2>&1 || [[ -x /opt/homebrew/opt/openjdk/bin/javac ]]
    else
        has javac
    fi
}
if java_ok; then
    ok "Java JDK 已安装"
elif ask "安装 Java JDK (Temurin)? AI 需要它来编译运行学生的代码"; then
    if [[ "$OS" == "Darwin" ]] && has brew; then
        echo "  brew install --cask temurin  (可能需要输入开机密码)"
        brew install --cask temurin && ok "Java JDK 已安装" || warn "Java 安装失败, 可稍后手动安装: https://adoptium.net"
    elif [[ "$OS" == "Darwin" ]]; then
        warn "没有 Homebrew, 请从 https://adoptium.net 下载安装 JDK"
    else
        warn "请手动安装, 例如: sudo apt install default-jdk"
    fi
else
    warn "跳过 Java JDK (AI 批改时只能阅读代码, 不能运行)"
fi

# Claude Code: Claude 桌面版自带一份, GUI 会自动找到
BUNDLED_CLAUDE="$(ls -d "$HOME/Library/Application Support/Claude/claude-code/"*/claude.app/Contents/MacOS/claude 2>/dev/null | tail -1 || true)"
if has claude || [[ -x "$HOME/.local/bin/claude" ]]; then
    ok "Claude Code 已安装"
elif [[ -n "$BUNDLED_CLAUDE" ]]; then
    ok "Claude Code: 使用 Claude 桌面版自带的版本 (第一次使用需在终端里 /login)"
elif ask "安装 Claude Code 命令行?"; then
    curl -fsSL https://claude.ai/install.sh | bash && ok "Claude Code 已安装" || warn "Claude Code 安装失败"
else
    warn "跳过 Claude Code"
fi

# Codex CLI (需要 Node.js)
if has codex; then
    ok "Codex 已安装"
elif ask "安装 Codex 命令行 (npm install -g @openai/codex)?"; then
    if ! has npm; then
        if [[ "$OS" == "Darwin" ]] && has brew; then
            brew install node
        else
            warn "需要先安装 Node.js: https://nodejs.org"
        fi
    fi
    if has npm; then
        npm install -g @openai/codex && ok "Codex 已安装 (第一次使用需在终端运行 codex 登录)" || warn "Codex 安装失败"
    fi
else
    warn "跳过 Codex"
fi

# ----------------------------------------------------------------------------
bold "5. 启动器"
RUN="$DIR/run_grader.sh"
cat > "$RUN" <<'EOF'
#!/usr/bin/env bash
# 用法: ./run_grader.sh        中文版
#       ./run_grader.sh en     English
#       ./run_grader.sh ko     한국어
cd "$(dirname "$0")"
case "${1:-}" in
    en|ko) exec "./.venv/bin/python" "canvas_grader_gui_$1.py" ;;
    *)     exec "./.venv/bin/python" canvas_grader_gui.py ;;
esac
EOF
chmod +x "$RUN"
ok "run_grader.sh [en|ko]"
if [[ "$OS" == "Darwin" ]]; then
    rm -f "$DIR/启动 Canvas Grader.command"   # 旧版启动器
    for pair in "中文:" "English:en" "한국어:ko"; do
        label="${pair%%:*}"; lang="${pair#*:}"
        script="canvas_grader_gui${lang:+_$lang}.py"
        [[ -f "$DIR/$script" ]] || continue
        LAUNCHER="$DIR/Canvas Grader ($label).command"
        printf '#!/usr/bin/env bash\n# 双击运行 Canvas Grader (%s)\ncd "$(dirname "$0")"\n./.venv/bin/python %s\n' \
            "$label" "$script" > "$LAUNCHER"
        chmod +x "$LAUNCHER"
        ok "Canvas Grader ($label).command"
    done
fi

# ----------------------------------------------------------------------------
bold "6. 检查"
"$VENV/bin/python" - "$APP" <<'EOF'
import sys
import tkinter, requests  # noqa: F401
from playwright.sync_api import sync_playwright  # noqa: F401
compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")
print("  \033[32m✔\033[0m tkinter / requests / playwright 可以导入, canvas_grader_gui.py 语法正确")
EOF

printf "\n\033[1m安装完成!\033[0m 启动方式:\n"
[[ "$OS" == "Darwin" ]] && echo "  • 在 Finder 里双击 \"Canvas Grader (中文).command\" (English / 한국어 同理)"
echo "  • 或在终端运行: $RUN   (英文版加参数 en, 韩文版加参数 ko)"
