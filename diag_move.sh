#!/usr/bin/env bash
# Diagnose nach Verschieben: warum zeigt das UI keine medien mehr?
set +e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/diag_move.txt"
cd "$HERE"

{
  echo "==== diag_move @ $(date) ===="
  echo "mediasearch-pfad: $HERE"
  echo

  echo "--- settings.json (welcher root ist konfiguriert?) ---"
  if [ -f settings.json ]; then
      cat settings.json
  else
      echo "settings.json existiert nicht"
  fi
  echo
  echo

  echo "--- data/ verzeichnis ---"
  if [ -d data ]; then
      ls -la data/
      echo
      echo "DB-files je root-hash:"
      for d in data/*/; do
          if [ -f "$d/files.db" ]; then
              sz=$(stat -c '%s' "$d/files.db")
              echo "  $d  files.db  ${sz} bytes"
          fi
      done
  else
      echo "data/ existiert nicht - ALLE DBs sind weg!"
  fi
  echo

  echo "--- erwarteter root-hash fuer settings.json-root ---"
  if [ -f settings.json ] && command -v python3 >/dev/null; then
      python3 - <<'PY'
import hashlib, json, pathlib
try:
    s = json.load(open("settings.json"))
    root = s.get("root", "").strip()
    if not root:
        print("kein root in settings.json")
    else:
        rp = str(pathlib.Path(root).expanduser().resolve())
        h = hashlib.blake2b(rp.encode(), digest_size=8).hexdigest()
        print(f"root        : {root}")
        print(f"resolved    : {rp}")
        print(f"hash (slot) : {h}")
        print(f"exists?     : {pathlib.Path(rp).is_dir()}")
        db = pathlib.Path('data')/h/'files.db'
        print(f"db path     : {db}")
        print(f"db exists?  : {db.is_file()} (size={db.stat().st_size if db.is_file() else 0})")
except Exception as e:
    print("ERR:", e)
PY
  fi
  echo

  echo "--- row-count pro DB (falls eine DB da ist) ---"
  for db in data/*/files.db; do
      [ -f "$db" ] || continue
      echo "$db:"
      if command -v sqlite3 >/dev/null; then
          sqlite3 "$db" "SELECT COUNT(*) AS files, \
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done \
              FROM files;" 2>&1
      else
          echo "  sqlite3 nicht installiert"
      fi
  done
  echo
  echo "==== ende ===="
} > "$OUT" 2>&1

echo "fertig - inhalt von $OUT zeigen"
