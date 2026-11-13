#!/usr/bin/env bash
# One-time setup on a Linux server with systemd. Run as your normal user from the
# project folder (it uses sudo only to install the timer):
#
#     bash deploy/setup.sh
#
# Safe to run again after a `git pull`.
set -euo pipefail

cd "$(dirname "$0")/.."
DIR="$(pwd)"
USER_NAME="$(whoami)"

case "$DIR" in *" "*) echo "The project path can't contain spaces: $DIR"; exit 1;; esac

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "Python 3.10 or newer is required (found $(python3 --version))."; exit 1; }

if [ ! -f .env ]; then
    echo "No .env file. Copy yours from your PC into $DIR first (see .env.example)."
    exit 1
fi
chmod 600 .env

echo "== Python environment"
if [ ! -d .venv ]; then
    python3 -m venv .venv || { echo "On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "== Tests"
.venv/bin/python -m pytest -q

echo "== Dry run (no orders)"
.venv/bin/python paper_trade.py alpaca --dry-run

echo "== Installing the daily timer"
systemd-analyze calendar "Mon..Fri 09:45 America/New_York" >/dev/null \
    || { echo "This systemd is too old for time zones in timers (needs v235+)."; exit 1; }
for f in stonks-paper.service stonks-paper.timer; do
    sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" "deploy/$f" \
        | sudo tee "/etc/systemd/system/$f" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now stonks-paper.timer

echo
systemctl list-timers stonks-paper.timer --no-pager
echo
echo "Done. Logs:   journalctl -u stonks-paper --since today"
echo "      Journal: $DIR/state/alpaca/journal.log"
echo "      Pause:   touch $DIR/STOP     (rm it to resume)"
echo "      Remove:  sudo systemctl disable --now stonks-paper.timer"
