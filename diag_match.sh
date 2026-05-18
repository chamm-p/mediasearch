#!/usr/bin/env bash
# Prueft byte-genau ob settings.json.root und data/<slot>/root.txt
# kompatibel sind (so dass root_slot() den slot findet).
set +e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
OUT="$HERE/diag_match.txt"

{
  echo "==== diag_match @ $(date) ===="
  echo

  echo "--- git status (commit muss 90e5aea oder neuer sein) ---"
  git log --oneline -3
  echo
  grep -n "_root_matches" common.py | head -3
  echo

  echo "--- inhalt settings.json (root-feld) ---"
  python3 - <<'PY'
import json, pathlib
try:
    s = json.load(open("settings.json"))
    r = s.get("root","")
    print(f"raw repr: {r!r}")
    print(f"len     : {len(r)}")
    p = pathlib.Path(r).expanduser()
    print(f"expand  : {p}")
    print(f"exists  : {p.is_dir()}")
    try:
        print(f"resolve : {p.resolve()}")
    except Exception as e:
        print(f"resolve : FEHLER {e}")
except Exception as e:
    print("ERR:", e)
PY
  echo

  echo "--- pro DB-slot: root.txt-vergleich ---"
  python3 - <<'PY'
import json, pathlib, hashlib
s = json.load(open("settings.json"))
cur = s.get("root","").strip()
cur_p = pathlib.Path(cur)
try: cur_resolve = str(cur_p.expanduser().resolve())
except Exception: cur_resolve = "(unresolvable)"
print(f"current root  : {cur!r}")
print(f"  expanduser  : {cur_p.expanduser()!s}")
print(f"  resolve     : {cur_resolve}")
print()
data = pathlib.Path("data")
if not data.is_dir():
    print("data/ existiert nicht"); raise SystemExit
for d in sorted(data.iterdir()):
    if not d.is_dir(): continue
    db = d / "mediasearch.db"
    rt = d / "root.txt"
    print(f"slot: {d.name}")
    print(f"  db   : {'OK' if db.is_file() else 'FEHLT'}",
          f"({db.stat().st_size} bytes)" if db.is_file() else "")
    if not rt.is_file():
        print("  root.txt : FEHLT"); print(); continue
    raw = rt.read_bytes()
    txt = raw.decode('utf-8', errors='replace').strip()
    print(f"  root.txt raw    : {raw!r}")
    print(f"  root.txt stripd : {txt!r}")
    # check matches
    m1 = (txt == cur) or (txt.rstrip('/') == cur.rstrip('/'))
    sto_p = pathlib.Path(txt).expanduser()
    m2 = str(sto_p).rstrip('/') == str(cur_p.expanduser()).rstrip('/')
    try: m3 = sto_p.resolve() == cur_p.resolve()
    except Exception: m3 = False
    print(f"  match-literal   : {m1}")
    print(f"  match-expanduser: {m2}")
    print(f"  match-resolve   : {m3}")
    print(f"  WUERDE MATCHEN  : {m1 or m2 or m3}")
    print()
PY
  echo

  echo "==== ende ===="
} > "$OUT" 2>&1
echo "fertig - inhalt von $OUT zeigen"
