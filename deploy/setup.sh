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

echo "== Installing the daily timers"
systemd-analyze calendar "Mon..Fri 09:45 America/New_York" >/dev/null \
    || { echo "This systemd is too old for time zones in timers (needs v235+)."; exit 1; }
WEB_DIR=/var/www/stonks
$SUDO mkdir -p "$WEB_DIR"
$SUDO chown "$USER_NAME" "$WEB_DIR"
command -v restorecon >/dev/null 2>&1 && $SUDO restorecon -R "$WEB_DIR"   # SELinux: let nginx read it

for f in stonks-paper.service stonks-paper.timer stonks-report.service stonks-report.timer; do
    sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" -e "s|__WEB__|$WEB_DIR|g" "deploy/$f" \
        | $SUDO tee "/etc/systemd/system/$f" >/dev/null
done
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now stonks-paper.timer stonks-report.timer

echo "== Web report"
# A random address, made once and kept, so scanners don't stumble onto the page.
mkdir -p state
if [ ! -f state/web-path ]; then
    echo "r-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')" > state/web-path
fi
WEB_PATH="$(cat state/web-path)"
$SUDO systemctl start stonks-report.service || echo "The report didn't generate; check: journalctl -u stonks-report"

# The server's public IP. Override it if this guesses wrong: STONKS_IP=1.2.3.4 bash deploy/setup.sh
IP="${STONKS_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
URL="http://${IP:-YOUR-SERVER-IP}/$WEB_PATH/report.txt"
# A separate nginx site that answers only requests addressed to the bare IP, so any
# websites already on this server (and whichever of them is the default) are untouched.
SITE="server {
    listen 80;
    server_name ${IP:-YOUR-SERVER-IP};

    location = /$WEB_PATH/report.txt {
        alias $WEB_DIR/report.txt;
        default_type text/plain;
        charset utf-8;
        add_header Cache-Control \"no-store\";
    }
    location / {
        return 404;
    }
}"
$SUDO rm -f /etc/nginx/default.d/stonks-report.conf   # older versions of this script put it there
WEB_OK=""
if [ -n "$IP" ] && command -v nginx >/dev/null 2>&1 && [ -d /etc/nginx/conf.d ]; then
    echo "$SITE" | $SUDO tee /etc/nginx/conf.d/stonks-report.conf >/dev/null
    if $SUDO nginx -t >/dev/null 2>&1; then
        $SUDO systemctl reload nginx
        sleep 1
        if curl -fsS -H "Host: $IP" "http://127.0.0.1/$WEB_PATH/report.txt" 2>/dev/null | head -1 | grep -q Generated; then
            WEB_OK=1
        fi
    else
        echo "nginx rejected the new site, so it was removed:"
        $SUDO rm -f /etc/nginx/conf.d/stonks-report.conf
        $SUDO nginx -t || true
    fi
fi
if [ -n "$WEB_OK" ]; then
    echo "Report page: $URL"
    echo "(Bookmark it. It updates after the 9:45 run and at 4:30 PM Eastern.)"
    echo "If it doesn't load from your phone, the public IP may differ from $IP:"
    echo "rerun with STONKS_IP=<your server's IP> bash deploy/setup.sh"
else
    echo "Couldn't set up the report page automatically. The file is written to"
    echo "$WEB_DIR/report.txt. To serve it, save this as /etc/nginx/conf.d/stonks-report.conf"
    echo "(with your server's public IP), then run: nginx -t && systemctl reload nginx"
    echo
    echo "$SITE"
    echo
    echo "The page will then be at: $URL"
fi

echo
systemctl list-timers 'stonks-*' --no-pager
echo
echo "Done. Logs:   journalctl -u stonks-paper --since today"
echo "      Journal: $DIR/state/alpaca/journal.log"
echo "      Pause:   touch $DIR/STOP     (rm it to resume)"
echo "      Remove:  sudo systemctl disable --now stonks-paper.timer stonks-report.timer"
