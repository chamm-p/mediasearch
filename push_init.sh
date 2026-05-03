#!/usr/bin/env bash
# Einmaliges Setup + erster Push nach github.com/chamm-p/mediasearch.
# Aufruf:  sudo ./push_init.sh
#
# (sudo wegen .git-Aufraeumen, das gehoert aktuell root)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

REMOTE="https://github.com/chamm-p/mediasearch.git"
NAME="chamm-p"
EMAIL="chamm-p@users.noreply.github.com"

echo "==> raeume altes .git auf"
rm -rf .git

# Ab hier wieder als der eigentliche User - sonst gehoeren neue Files root
REAL_USER="${SUDO_USER:-$USER}"
run() { sudo -u "$REAL_USER" "$@"; }

echo "==> git init"
run git init -b main
run git config user.name  "$NAME"
run git config user.email "$EMAIL"

echo "==> git add"
run git add .

echo "==> Vorschau (das wird committed):"
run git status --short

echo
read -p "OK so? [y/N] " ans
[[ "$ans" =~ ^[yY]$ ]] || { echo "abgebrochen."; exit 1; }

echo "==> commit"
run git commit -m "initial commit: mediasearch - LLM vision tagging + search UI"

echo "==> remote setzen"
run git remote add origin "$REMOTE" 2>/dev/null || run git remote set-url origin "$REMOTE"

echo "==> push (erfordert gh-auth)"
run git push -u origin main

echo
echo "fertig. Repo: $REMOTE"
