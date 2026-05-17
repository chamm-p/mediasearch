#!/usr/bin/env bash
# Benennt den DB-Ordner auf den aktuell erwarteten Root-Hash um.
# Voraussetzung: in data/ liegt genau EIN alter Hash-Ordner mit files.db.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ ! -f settings.json ]; then
    echo "FEHLER: settings.json nicht da"; exit 1
fi

NEW_HASH="$(python3 - <<'PY'
import hashlib, json, pathlib
s = json.load(open("settings.json"))
root = s.get("root","").strip()
rp = str(pathlib.Path(root).expanduser().resolve())
print(hashlib.blake2b(rp.encode(), digest_size=8).hexdigest())
PY
)"

echo "erwarteter neuer hash : $NEW_HASH"

if [ -f "data/$NEW_HASH/files.db" ]; then
    echo "data/$NEW_HASH/files.db existiert bereits - nichts zu tun."
    exit 0
fi

# alte hash-ordner finden (alles unter data/ das eine files.db hat)
OLD_HASHES=()
for d in data/*/; do
    name="$(basename "$d")"
    [ "$name" = "$NEW_HASH" ] && continue
    if [ -f "$d/files.db" ]; then
        OLD_HASHES+=("$name")
    fi
done

if [ ${#OLD_HASHES[@]} -eq 0 ]; then
    echo "kein alter DB-ordner gefunden in data/"; exit 1
fi
if [ ${#OLD_HASHES[@]} -gt 1 ]; then
    echo "mehrere DB-ordner - manuell entscheiden welcher:"
    for h in "${OLD_HASHES[@]}"; do
        sz=$(stat -c '%s' "data/$h/files.db")
        echo "  data/$h/files.db  (${sz} bytes)"
    done
    exit 1
fi

OLD="${OLD_HASHES[0]}"
echo "rename: data/$OLD  ->  data/$NEW_HASH"
mv "data/$OLD" "data/$NEW_HASH"
echo "fertig. jetzt serve.py neu starten (./restart_serve.sh)."
