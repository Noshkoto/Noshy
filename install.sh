#!/bin/sh
# Noshy one-liner installer
# curl -fsSL https://raw.githubusercontent.com/noshkoto/Noshy/main/install.sh | sh
set -e

echo "Installing Noshy — persistent memory for AI agents..."
echo ""

# Check Python
python3 --version >/dev/null 2>&1 || { echo "Python 3.10+ required"; exit 1; }

# Create directory
NOSHY_DIR="${NOSHY_DIR:-$HOME/.noshy}"
mkdir -p "$NOSHY_DIR"

# Clone or download
if [ -d "$NOSHY_DIR/src/.git" ]; then
    echo "Updating existing install..."
    (cd "$NOSHY_DIR/src" && git pull) || echo "Update failed — continuing with existing copy"
elif [ -d "$NOSHY_DIR/src" ]; then
    echo "Existing non-git install detected at $NOSHY_DIR/src — leaving in place"
else
    echo "Downloading Noshy..."
    if command -v git >/dev/null 2>&1; then
        git clone --depth 1 https://github.com/noshkoto/Noshy.git "$NOSHY_DIR/src"
    else
        echo "Git not available. Downloading tarball..."
        TMP_TGZ="$(mktemp -t noshy.XXXXXX.tar.gz)"
        curl -fsSL https://github.com/noshkoto/Noshy/archive/refs/heads/main.tar.gz -o "$TMP_TGZ"
        TMP_DIR="$(mktemp -d -t noshy.XXXXXX)"
        tar xzf "$TMP_TGZ" -C "$TMP_DIR"
        mv "$TMP_DIR/Noshy-main" "$NOSHY_DIR/src"
        rm -rf "$TMP_TGZ" "$TMP_DIR"
    fi
fi

# Optional: install fastembed for local embeddings
echo ""
printf "Install fastembed for local embeddings (no API key needed)? [Y/n] "
read -r answer
case "$answer" in
    n|N|no|NO) ;;
    *)
        pip3 install --user fastembed && echo "fastembed installed" || echo "fastembed install failed (continuing)"
        ;;
esac

echo ""
echo "=== Noshy installed ==="
echo ""
echo "Start with:"
echo "  python3 $NOSHY_DIR/src/server.py http"
echo ""
echo "Or as MCP server:"
echo "  python3 $NOSHY_DIR/src/server.py mcp"
echo ""
echo "Set NOSHY_EMBED_PROVIDER=openai for OpenAI embeddings"
echo "Set NOSHY_EMBED_PROVIDER=fastembed for local embeddings"
echo "Set NOSHY_EMBED_PROVIDER=none to disable embeddings (keyword search only)"
