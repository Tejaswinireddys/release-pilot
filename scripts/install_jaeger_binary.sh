#!/usr/bin/env bash
# scripts/install_jaeger_binary.sh — download Jaeger all-in-one into ./bin/
#
# This is OPTIONAL. Release Pilot works fine without Jaeger:
#   - With no Jaeger: traces go to ./traces/last_run.txt (file mode)
#   - With Jaeger:    traces go to the Jaeger UI at http://localhost:16686
#
# Usage:
#   bash scripts/install_jaeger_binary.sh          # latest stable
#   JAEGER_VERSION=1.62.0 bash scripts/install_jaeger_binary.sh
#
# After installation, add ./bin to your PATH:
#   export PATH="$PWD/bin:$PATH"     # add to ~/.zshrc or ~/.bashrc to persist
# Then restart start_demo.sh — it will auto-detect jaeger-all-in-one and use OTLP mode.
set -euo pipefail

JAEGER_VERSION="${JAEGER_VERSION:-1.62.0}"
BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin"

_GREEN='\033[0;32m'; _YELLOW='\033[1;33m'; _RED='\033[0;31m'; _NC='\033[0m'
ok()   { echo -e "${_GREEN}[ok]${_NC}  $*"; }
warn() { echo -e "${_YELLOW}[warn]${_NC} $*"; }
die()  { echo -e "${_RED}[error]${_NC} $*" >&2; exit 1; }

# Detect OS and architecture
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Linux)  OS_TAG="linux"  ;;
    Darwin) OS_TAG="darwin" ;;
    *)      die "Unsupported OS: $OS (supported: Linux, Darwin/macOS)" ;;
esac

case "$ARCH" in
    x86_64|amd64)  ARCH_TAG="amd64" ;;
    arm64|aarch64) ARCH_TAG="arm64" ;;
    *)             die "Unsupported architecture: $ARCH (supported: x86_64, arm64)" ;;
esac

TARBALL="jaeger-${JAEGER_VERSION}-${OS_TAG}-${ARCH_TAG}.tar.gz"
URL="https://github.com/jaegertracing/jaeger/releases/download/v${JAEGER_VERSION}/${TARBALL}"
BINARY_NAME="jaeger-all-in-one"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Jaeger all-in-one installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Version  : $JAEGER_VERSION"
echo "  Platform : ${OS_TAG}/${ARCH_TAG}"
echo "  Target   : $BIN_DIR/$BINARY_NAME"
echo ""

mkdir -p "$BIN_DIR"

# Check if already installed
if [ -x "$BIN_DIR/$BINARY_NAME" ]; then
    INSTALLED_VERSION="$("$BIN_DIR/$BINARY_NAME" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo unknown)"
    warn "$BINARY_NAME already installed (version: $INSTALLED_VERSION)"
    warn "Delete $BIN_DIR/$BINARY_NAME and re-run to reinstall."
    echo ""
    _print_next_steps "$BIN_DIR"
    exit 0
fi

# Download
echo "Downloading $TARBALL..."
TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT

if command -v curl &>/dev/null 2>&1; then
    curl -fsSL -o "$TMPDIR_WORK/$TARBALL" "$URL" \
        || die "Download failed. Check version: https://github.com/jaegertracing/jaeger/releases"
elif command -v wget &>/dev/null 2>&1; then
    wget -q -O "$TMPDIR_WORK/$TARBALL" "$URL" \
        || die "Download failed. Check version: https://github.com/jaegertracing/jaeger/releases"
else
    die "Neither curl nor wget found. Install one and retry."
fi
ok "Downloaded $TARBALL"

# Extract
echo "Extracting..."
tar -xzf "$TMPDIR_WORK/$TARBALL" -C "$TMPDIR_WORK"

# Find the binary (directory structure varies by Jaeger version)
FOUND_BIN="$(find "$TMPDIR_WORK" -name "$BINARY_NAME" -type f 2>/dev/null | head -1)"
if [ -z "$FOUND_BIN" ]; then
    # Jaeger 2.x names the binary just "jaeger"
    FOUND_BIN="$(find "$TMPDIR_WORK" -name "jaeger" -type f 2>/dev/null | head -1)"
    if [ -n "$FOUND_BIN" ]; then
        BINARY_NAME="jaeger"
        warn "Binary is named 'jaeger' (v2.x) rather than 'jaeger-all-in-one'."
        warn "start_demo.sh checks for 'jaeger-all-in-one'; creating a symlink."
        cp "$FOUND_BIN" "$BIN_DIR/jaeger"
        ln -sf "$BIN_DIR/jaeger" "$BIN_DIR/jaeger-all-in-one"
    else
        die "Could not find jaeger or jaeger-all-in-one in the downloaded tarball."
    fi
else
    cp "$FOUND_BIN" "$BIN_DIR/$BINARY_NAME"
fi

chmod +x "$BIN_DIR/$BINARY_NAME"
ok "Installed to $BIN_DIR/$BINARY_NAME"

# Verify
VERSION_OUT="$("$BIN_DIR/$BINARY_NAME" version 2>/dev/null || echo 'version check not supported')"
ok "Verification: $VERSION_OUT"

_print_next_steps() {
    local bin_dir="$1"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Next steps"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  1. Add ./bin to your PATH (for this shell session):"
    echo "       export PATH=\"${bin_dir}:\$PATH\""
    echo ""
    echo "  2. Add it permanently (choose your shell):"
    echo "       echo 'export PATH=\"${bin_dir}:\$PATH\"' >> ~/.zshrc   # zsh"
    echo "       echo 'export PATH=\"${bin_dir}:\$PATH\"' >> ~/.bashrc  # bash"
    echo ""
    echo "  3. Restart start_demo.sh — it will auto-detect jaeger-all-in-one"
    echo "     and switch to OTLP mode, giving you the full Jaeger UI at"
    echo "     http://localhost:16686"
    echo ""
    echo "  Without Jaeger, the project runs fine in file mode:"
    echo "     traces go to ./traces/last_run.txt after each pipeline run."
    echo ""
}

_print_next_steps "$BIN_DIR"
