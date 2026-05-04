#!/usr/bin/env bash
# Zeigt nur den aktuellen DB-State - ohne irgendetwas zu aendern.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ ! -x .venv/bin/python ]; then
    echo "FEHLER: .venv fehlt - erst ./repair_dedupe.sh laufen lassen"; exit 1
fi

.venv/bin/python <<'PY'
import sqlite3, glob, os, sys
sys.path.insert(0, ".")
from pathlib import Path
from common import init_db
slots = sorted(glob.glob("data/*/mediasearch.db"))
if not slots:
    print("Keine DB gefunden unter data/*/")
    raise SystemExit(0)
for db in slots:
    slot = os.path.dirname(db)
    rt = os.path.join(slot, "root.txt")
    root = open(rt).read().strip() if os.path.exists(rt) else "?"
    # Migration triggern, damit alte Slots die content_hash/phash_int-Spalten kriegen
    if root and root != "?" and Path(root).is_dir():
        try:
            init_db(Path(root).resolve(), force=True)
        except Exception as e:
            print(f"  init_db fuer {slot} fehlgeschlagen: {e}")
    c = sqlite3.connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(files)").fetchall()}
    if "content_hash" not in cols or "phash_int" not in cols:
        print(f"\nSlot {slot} hat noch keine Hash-Spalten - skip "
              f"(Root: {root})")
        c.close()
        continue
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
    # Schnelle Hochrechnung: bei aktueller phash-Rate, wie lang noch?
    if pending and total:
        print(f"\n  Bei 20/s noch ca. {pending/20/60:5.1f} min - bei 100/s ca. {pending/100/60:5.1f} min")
PY

# Plus: zeige auch die Liste der bisher gefailten dedupe-Files (falls
# dedupe.py einzelne Files nicht lesen konnte)
echo
echo "--> Files ohne lesbaren Pfad (nicht hashbar):"
.venv/bin/python <<'PY'
import sqlite3, glob, os, sys
sys.path.insert(0, ".")
from pathlib import Path
from common import decode_surrogates
slots = sorted(glob.glob("data/*/mediasearch.db"))
for db in slots:
    slot = os.path.dirname(db)
    rt = os.path.join(slot, "root.txt")
    root = open(rt).read().strip() if os.path.exists(rt) else ""
    if not root: continue
    c = sqlite3.connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(files)").fetchall()}
    if "content_hash" not in cols:
        print(f"  {slot}: keine content_hash-Spalte (alte DB) - skip")
        c.close()
        continue
    rows = c.execute(
        "SELECT id, rel_path FROM files WHERE content_hash IS NULL OR content_hash = '' "
        "LIMIT 10"
    ).fetchall()
    n_total = c.execute(
        "SELECT COUNT(*) FROM files WHERE content_hash IS NULL OR content_hash = ''"
    ).fetchone()[0]
    c.close()
    if n_total == 0:
        print(f"  {slot}: alle Files haben content_hash")
        continue
    print(f"  {slot}: {n_total} Files ohne content_hash, erste 10:")
    for row_id, rel in rows:
        p = Path(root) / decode_surrogates(rel)
        ok = "OK " if p.exists() else "FEHLT"
        print(f"    [{ok}] id={row_id}  {rel}")
PY
