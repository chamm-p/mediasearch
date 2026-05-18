#!/usr/bin/env bash
# Laedt alle wheels aus requirements.txt nach vendor/ herunter.
# Einmalig auf einem rechner mit internet ausfuehren.
# Danach baut run.sh die venv offline aus diesem cache, wenn vendor/
# vorhanden ist.
#
# Hinweis: wheels sind plattform-spezifisch (linux x86_64 vs mac arm etc.).
# Auf der zielmaschine muss dieselbe plattform/python-major-version laufen.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

mkdir -p vendor

if [ ! -d .venv ]; then
    echo "kein .venv da - lege temporaer eins an fuer pip-download..."
    python3 -m venv .venv-tmp
    PIP=.venv-tmp/bin/pip
    cleanup() { rm -rf .venv-tmp; }
    trap cleanup EXIT
else
    PIP=.venv/bin/pip
fi

echo "lade wheels nach vendor/ ..."
"$PIP" download \
    --dest vendor \
    --only-binary=:all: \
    -r requirements.txt

echo
echo "fertig. inhalt vendor/:"
ls -lh vendor/ | head -30
echo
echo "groesse: $(du -sh vendor/ | cut -f1)"
echo
echo "ab jetzt kann run.sh die venv offline neu bauen."
echo "transport-tipp: vendor/ mit ins mediasearch-archiv packen."
