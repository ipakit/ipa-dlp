#!/bin/bash
#
# Install the ipa-dlp wrapper into the current user's home directory.
# No sudo is needed here; the first decryption setup asks for admin once.
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_SETUP=1
ADD_PATH=0

usage() {
    cat <<'EOF'
Usage: ./install.sh [--no-run] [--add-path]

  --no-run     install files only; do not run `ipa-dlp setup`
  --add-path   append ~/.local/bin to PATH in ~/.zshrc if missing
EOF
}

for arg in "$@"; do
    case "$arg" in
        --no-run) RUN_SETUP=0 ;;
        --add-path) ADD_PATH=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ipa-dlp only supports macOS." >&2
    exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3 is required. Install the Xcode Command Line Tools with:" >&2
    echo "  xcode-select --install" >&2
    exit 1
fi

"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is required (found %s)" % sys.version.split()[0])
PY

mkdir -p "$HOME/.local/bin"
install -m 755 "$SRC_DIR/ipa-dlp" "$HOME/.local/bin/ipa-dlp"
install -m 644 "$SRC_DIR/ipa_dlp.py" "$HOME/.local/bin/ipa_dlp.py"

# Make `import ipa_dlp` work from arbitrary scripts, not just from the
# ipa-dlp entrypoint (whose sys.path[0] is ~/.local/bin).
USER_SITE="$("$PYTHON" -m site --user-site 2>/dev/null || true)"
if [ -n "$USER_SITE" ]; then
    mkdir -p "$USER_SITE"
    printf '%s\n' "$HOME/.local/bin" > "$USER_SITE/ipa_dlp.pth"
else
    echo "Warning: could not find the Python user site-packages directory." >&2
    echo "         Set PYTHONPATH=$HOME/.local/bin to import ipa_dlp." >&2
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo
        echo "Note: $HOME/.local/bin is not on PATH in this shell."
        echo "You can either use the full path:"
        echo "  $HOME/.local/bin/ipa-dlp"
        echo "or re-run with --add-path to add it to ~/.zshrc."
        ;;
esac

if [ "$ADD_PATH" = 1 ]; then
    ZSHRC="$HOME/.zshrc"
    if ! grep -q 'ipa-dlp: added by install.sh' "$ZSHRC" 2>/dev/null; then
        {
            echo
            echo '# ipa-dlp: added by install.sh'
            echo 'export PATH="$HOME/.local/bin:$PATH"'
        } >> "$ZSHRC"
        echo "Added ~/.local/bin to PATH in $ZSHRC (open a new Terminal)."
    fi
fi

echo
echo "Installed:"
echo "  $HOME/.local/bin/ipa-dlp"
echo "  $HOME/.local/bin/ipa_dlp.py"
if [ -n "${USER_SITE:-}" ]; then
    echo "  $USER_SITE/ipa_dlp.pth"
fi
echo

if [ "$RUN_SETUP" = 1 ]; then
    exec "$HOME/.local/bin/ipa-dlp" setup
fi

echo "Next: $HOME/.local/bin/ipa-dlp setup"
