#!/usr/bin/env bash
# Laedt Firefox als portablen Tarball nach <scripts>/browser/firefox/.
# Linux x86_64. Fuer Windows oder Mac einfach manuell die entsprechenden
# portable-Versionen in <scripts>/browser/ ablegen.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/browser"
LANG_TAG="${LANG_TAG:-de}"   # optional via env: LANG_TAG=en setzen

mkdir -p "$DEST"
cd "$DEST"

if [ -x "firefox/firefox" ]; then
    echo "Firefox bereits installiert: $DEST/firefox/firefox"
    exit 0
fi

URL="https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64&lang=${LANG_TAG}"
echo "lade Firefox-Tarball nach $DEST ..."
curl -L --progress-bar -o firefox.tar.xz "$URL"
echo "entpacke ..."
tar xf firefox.tar.xz
rm firefox.tar.xz
echo
echo "fertig: $DEST/firefox/firefox"
echo "Profil wird unter \$HOME angelegt - falls du auch das Profil"
echo "portabel halten willst, run.sh nutzt automatisch ein Profil"
echo "unter $DEST/profile/."
