#!/usr/bin/env bash
# One-time setup for claude-usage
set -e

cd "$(dirname "$0")"

echo "=== Claude Usage Checker Setup ==="
echo

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+."
    exit 1
fi

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install --quiet playwright httpx curl_cffi

echo "Installing Chromium browser for Playwright..."
playwright install chromium

echo
echo "Setup complete!"
echo
echo "Next steps:"
echo "  1. Log in to your accounts:"
echo "     source .venv/bin/activate && python3 check_usage.py setup"
echo
echo "  2. Then check usage anytime:"
echo "     source .venv/bin/activate && python3 check_usage.py check"
echo
echo "  Tip: add an alias to your shell profile:"
echo "     alias claude-usage='source ~/claude-usage/.venv/bin/activate && python3 ~/claude-usage/check_usage.py check && deactivate'"
