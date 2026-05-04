#!/usr/bin/env bash
# Reparatur-Helper fuer fehlende dedupe-Hashes.
#
# 1) Installiert imagehash + numpy ins venv (waren oft nicht da)
# 2) Zeigt aktuellen DB-State pro Slot (total / mit content_hash / mit phash_int)
# 3) Optional: dedupe direkt anstossen wenn ein Wurzelverzeichnis gegeben
#
# Aufruf:
#   ./repair_dedupe.sh                  # nur installieren + Status zeigen
#   ./repair_dedupe.sh /pfad/zu/medien  # zusaetzlich dedupe ausfuehren

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "==============================================="
echo " mediasearch dedupe-Repair"
echo "==============================================="

# 1) venv check
if [ ! -d .venv ]; then
    echo "FEHLER: .venv fehlt. Erst './run.sh ui' starten oder 'python3 -m venv .venv'"
    exit 1
fi

# 2) ownership check - falls .venv root gehoert, Hinweis und ggf. fixen
VENV_OWNER=$(stat -c '%U' .venv 2>/dev/null || echo "?")
if [ "$VENV_OWNER" = "root" ] && [ "$(whoami)" != "root" ]; then
    echo "Hinweis: .venv gehoert root (vermutlich frueher mit sudo angefasst)."
    echo "Ich fixe die Ownership einmalig (benoetigt Passwort)..."
    sudo chown -R "$USER":"$(id -gn)" .venv
    echo "  done."
    echo
fi

echo "--> Installiere/aktualisiere requirements (imagehash, numpy, ...)"
.venv/bin/pip install -q -r requirements.txt
echo

echo "--> Pruefe Pakete"
.venv/bin/pip list 2>/dev/null | grep -Ei "^(imagehash|numpy|pillow)\s" || true
echo

echo "--> DB-State pro Slot:"
.venv/bin/python <<'PY'
import sqlite3, glob, os
slots = sorted(glob.glob("data/*/mediasearch.db"))
if not slots:
    print("  (keine DB gefunden unter data/*/)")
for db in slots:
    slot = os.path.dirname(db)
    root_file = os.path.join(slot, "root.txt")
    root = "?"
    if os.path.exists(root_file):
        root = open(root_file).read().strip()
    c = sqlite3.connect(db)
    r = c.execute("SELECT COUNT(*) AS total, "
                  "COUNT(CASE WHEN content_hash IS NOT NULL AND content_hash <> '' THEN 1 END) AS chash, "
                  "COUNT(phash_int) AS phash FROM files").fetchone()
    pending = c.execute("SELECT COUNT(*) FROM files WHERE content_hash IS NULL OR content_hash = '' OR phash_int IS NULL").fetchone()[0]
    c.close()
    print(f"  Slot: {slot}")
    print(f"    Root:    {root}")
    print(f"    Total:   {r[0]}")
    print(f"    chash:   {r[1]}  ({100*r[1]/r[0] if r[0] else 0:.0f}%)")
    print(f"    phash:   {r[2]}  ({100*r[2]/r[0] if r[0] else 0:.0f}%)")
    print(f"    Pending: {pending} -> diese werden bei dedupe verarbeitet")
PY
echo

# 2) Optional dedupe starten wenn Pfad gegeben
if [ $# -ge 1 ]; then
    ROOT="$1"; shift
    echo "--> Starte dedupe fuer $ROOT"
    echo "    Bei Strg+C werden gerade fertige Batches (alle 50 Files) gespeichert."
    echo
    exec .venv/bin/python dedupe.py "$ROOT" "$@"
fi

echo "Fertig. Wenn die phash-Quote bei <100% war:"
echo "  ./repair_dedupe.sh /pfad/zum/wurzel    (laeuft dedupe, schreibt fehlende Hashes)"
echo "Beim NAECHSTEN Run wird dann tatsaechlich geskippt."
