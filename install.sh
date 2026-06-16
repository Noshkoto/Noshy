#!/bin/sh
# Noshy one-liner installer
# curl -fsSL https://raw.githubusercontent.com/noshkoto/Noshy/main/install.sh | sh
set -e

echo "Installing Noshy — persistent memory for AI agents..."
echo ""

# Check Python
python3 --version >/dev/null 2>&1 || { echo "Python 3.10+ required"; exit 1; }

# Create directory
AI_DIR="${AI_DIR:-$HOME/.aion}"
mkdir -p "$AI_DIR"

# Clone or download
if [ -d "$AI_DIR/src" ]; then
    echo "Updating existing install..."
    cd "$AI_DIR/src" && git pull 2>/dev/null || true
else
    echo "Downloading Noshy..."
    git clone --depth 1 https://github.com/noshkoto/Noshy.git "$AI_DIR/src" 2>/dev/null || {
        echo "Git not available. Downloading tarball..."
        curl -fsSL https://github.com/noshkoto/Noshy/archive/refs/heads/main.tar.gz | tar xz -C /tmp/
        mv /tmp/aion-main "$AI_DIR/src"
    }
fi

cd "$AI_DIR/src"

# Optional: install fastembed for local embeddings
echo ""
echo "Install fastembed for local embeddings (no API key needed)? [Y/n]"
read -r answer
if [ "$answer" != "n" ] && [ "$answer" != "N" ]; then
    pip3 install --user fastembed
    echo "fastembed installed"
fi

echo ""
echo "=== Noshy installed ==="
echo ""
echo "Start with:"
echo "  python3 $AI_DIR/src/server.py http"
echo ""
echo "Or as MCP server:"
echo "  python3 $AI_DIR/src/server.py mcp"
echo ""
echo "Set NOSHY_EMBED_PROVIDER=openai for OpenAI embeddings"
echo "Set NOSHY_EMBED_PROVIDER=fastembed for local embeddings"
