#!/bin/zsh
#
# Double-click launcher for the popup-driven ipa-dlp wizard.
#
set -u

# Finder launches .command files with a minimal PATH, so look in the usual
# install locations instead of relying on `command -v` alone.
IPADLP=""
if command -v ipa-dlp >/dev/null 2>&1; then
    IPADLP="$(command -v ipa-dlp)"
else
    for candidate in \
        "$HOME/.local/bin/ipa-dlp" \
        "/opt/homebrew/bin/ipa-dlp" \
        "/usr/local/bin/ipa-dlp"
    do
        if [ -x "$candidate" ]; then
            IPADLP="$candidate"
            break
        fi
    done
fi

if [ -z "$IPADLP" ]; then
    /usr/bin/osascript -e 'display alert "ipa-dlp is not installed" message "Run install.sh from the ipa-dlp folder, or: brew install bejminliou/ipa-dlp/ipa-dlp" as critical buttons {"OK"} default button "OK"' >/dev/null 2>&1 || true
    echo "ipa-dlp is not installed. Run install.sh or brew install bejminliou/ipa-dlp/ipa-dlp." >&2
    read -r "?Press Return to close..."
    exit 1
fi

"$IPADLP" wizard
status=$?
if [ "$status" -ne 0 ]; then
    echo
    read -r "?Press Return to close..."
fi
exit "$status"
