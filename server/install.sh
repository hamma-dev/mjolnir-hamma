#!/usr/bin/env bash
#
# Install symlinks for the server-side mjolnir-hamma scripts so they can be
# invoked by name from anywhere on PATH (e.g. `mjol_array --status -a hamma`
# instead of `./mjol_array.py`).
#
# Usage:
#     bash server/install.sh                      # default: /usr/local/bin (uses sudo)
#     bash server/install.sh ~/.local/bin         # user-local, no sudo
#     bash server/install.sh /custom/bin          # custom dir
#
# Idempotent: re-running just refreshes the symlinks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET_DIR="${1:-/usr/local/bin}"

# `sudo` only when we don't own the target dir
if [[ -w "$TARGET_DIR" ]] || { [[ ! -e "$TARGET_DIR" ]] && [[ -w "$(dirname "$TARGET_DIR")" ]]; }; then
    SUDO=""
else
    SUDO="sudo"
fi

$SUDO mkdir -p "$TARGET_DIR"

for tool in mjol_array webgen; do
    src="$SCRIPT_DIR/${tool}.py"
    dest="$TARGET_DIR/$tool"

    if [[ ! -f "$src" ]]; then
        echo "[FAIL] $src not found, skipping" >&2
        continue
    fi

    chmod +x "$src"
    $SUDO ln -sfn "$src" "$dest"
    echo "[OK] $dest -> $src"
done

case ":$PATH:" in
    *":$TARGET_DIR:"*) ;;
    *)
        echo
        echo "Note: $TARGET_DIR is not on \$PATH. Add it to your shell rc, e.g.:"
        echo "    echo 'export PATH=\"$TARGET_DIR:\$PATH\"' >> ~/.bashrc"
        ;;
esac
