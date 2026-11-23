#!/usr/bin/env bash
# One-time setup: virtual environment + packages + .env file.
set -e
cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
    cp env.example .env
    echo "Created .env  ->  open it and fill in your values:  nano .env"
fi

echo
echo "Setup finished. To run:"
echo "  source .venv/bin/activate"
echo "  python3 scrape.py"
echo "  python3 aws_console_login.py --dry-run"
