#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/bot-timetable}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as the VM login user, not root." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  git \
  build-essential \
  sqlite3 \
  curl \
  ca-certificates

if [[ ! -d "$APP_DIR" ]]; then
  sudo mkdir -p "$APP_DIR"
  sudo chown "$USER":"$USER" "$APP_DIR"
fi

cd "$APP_DIR"

if [[ ! -d .git ]]; then
  echo "Clone the private repo into $APP_DIR first, then rerun this script." >&2
  echo "Example: git clone <PRIVATE_REPO_URL> $APP_DIR" >&2
  exit 1
fi

"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium

mkdir -p data/backup
chmod 700 data data/backup

echo "Bootstrap complete."
echo "Next:"
echo "  1. Copy .env and auth_state_lms.json into $APP_DIR"
echo "  2. chmod 600 .env auth_state_*.json"
echo "  3. sudo cp deploy/bot-timetable.service /etc/systemd/system/bot-timetable.service"
echo "  4. sudo systemctl daemon-reload"
echo "  5. sudo systemctl enable --now bot-timetable"
