#!/usr/bin/env bash
# Zeigt nur den aktuellen DB-State - ohne irgendetwas zu aendern.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ ! -x .venv/bin/python ]; then
    echo "FEHLER: .venv fehlt - erst ./repair_dedupe.sh laufen lassen"; exit 1
fi

.venv/bin/python <<'PY'
import sqlite3, glob, os
slots = sorted(glob.glob("data/*/mediasearch.db"))
if not slots:
    print("Keine DB gefunden unter data/*/")
    raise SystemExit(0)
for db in slots:
    slot = os.path.dirname(db)
    rt = os.path.join(slot, "root.txt")
    root = open(rt).read().strip() if os.path.exists(rt) else "?"
    c = sqlite3.connect(db)
    total = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chash = c.execute("SELECT COUNT(*) FROM files WHERE content_hash IS NOT NULL AND content_hash <> ''").fetchone()[0]
    phash = c.execute("SELECT COUNT(*) FROM files WHERE phash_int IS NOT NULL").fetchone()[0]
    only_chash = c.execute("SELECT COUNT(*) FROM files WHERE content_hash <> '' AND phash_int IS NULL").fetchone()[0]
    only_phash = c.execute("SELECT COUNT(*) FROM files WHERE (content_hash IS NULL OR content_hash = '') AND phash_int IS NOT NULL").fetchone()[0]
    none = c.execute("SELECT COUNT(*) FROM files WHERE (content_hash IS NULL OR content_hash = '') AND phash_int IS NULL").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM files WHERE content_hash IS NULL OR content_hash = '' OR phash_int IS NULL").fetchone()[0]
    by_type = c.execute("SELECT type, COUNT(*) FROM files GROUP BY type").fetchall()
    c.close()
    print(f"\nSlot:     {slot}")
    print(f"Root:     {root}")
    print(f"Total:    {total}")
    for t, n in by_type:
        print(f"  {t}: {n}")
    print(f"\nHash-Status:")
    print(f"  beide gesetzt:        {total - pending}  ({100*(total-pending)/total if total else 0:5.1f}%)")
    print(f"  nur content_hash:     {only_chash}")
    print(f"  nur phash_int:        {only_phash}")
    print(f"  keiner gesetzt:       {none}")
    print(f"  -> dedupe Pending:    {pending}")
PY
