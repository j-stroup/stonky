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

# Use sudo only when not already root (minimal servers may not have sudo).
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

# Find a Python that's new enough. The system "python3" is often older than the
# one you installed (e.g. Rocky/Alma 9 ship 3.9 but offer python3.12 as a package).
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        PY="$candidate"; break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.11 or newer is required (python3 is $(python3 --version 2>&1))."
    echo "Install one, then run this again:"
    echo "  Rocky/Alma/RHEL 9:  dnf install -y python3.12"
    echo "  Ubuntu/Debian:      apt install -y python3.12 python3.12-venv  (or the newest your release offers)"
    exit 1
fi
echo "Using $PY ($($PY --version))"

# SELinux (Rocky/Alma/RHEL) usually blocks systemd from running programs inside
# home folders, which shows up later as "status=203/EXEC". /opt is fine.
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    case "$DIR" in /root/*|/home/*)
        echo "SELinux is on and the project is in $DIR. The timer would likely fail to start."
        echo "Move it first, then rerun:  mv \"$DIR\" /opt/stonks && cd /opt/stonks && bash deploy/setup.sh"
        exit 1;;
    esac
fi

if [ ! -f .env ]; then
    echo "No .env file. Copy yours from your PC into $DIR first (see .env.example)."
    exit 1
fi
chmod 600 .env

echo "== Python environment"
if [ ! -d .venv ]; then
    "$PY" -m venv .venv || { echo "Couldn't create a virtual environment. On Debian/Ubuntu: apt install python3-venv"; exit 1; }
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
        | $SUDO tee "/etc/systemd/system/$f" >/dev/null
done
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now stonks-paper.timer

echo
systemctl list-timers stonks-paper.timer --no-pager
echo
echo "Done. Logs:   journalctl -u stonks-paper --since today"
echo "      Journal: $DIR/state/alpaca/journal.log"
echo "      Pause:   touch $DIR/STOP     (rm it to resume)"
echo "      Remove:  sudo systemctl disable --now stonks-paper.timer"
