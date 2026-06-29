#!/usr/bin/env bash
# change_root.sh <neuer-medien-pfad>
#
# Setzt den Medien-Root sowohl in settings.json als auch in der
# zugehoerigen data/<slot>/root.txt um. Damit bleibt die DB an den
# bisherigen Slot gebunden und die Tags ueberleben den Move.
#
# Findet den richtigen Slot automatisch:
#  1. Wenn nur EIN slot mit DB existiert -> der.
#  2. Wenn mehrere existieren -> der dessen root.txt zum ALTEN root
#     aus settings.json passt.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ $# -ne 1 ]; then
    echo "usage: $0 /neuer/medien/pfad"
    exit 1
fi
NEW="$1"

if [ ! -d "$NEW" ]; then
    echo "FEHLER: '$NEW' existiert nicht oder ist kein verzeichnis." >&2
    # CHANGE_ROOT_FORCE=1 erzwingt das setzen auch wenn pfad nicht existiert
    # (z.B. wenn der mount noch nicht aktiv ist).
    if [ "${CHANGE_ROOT_FORCE:-0}" != "1" ]; then
        echo "(falls du das absichtlich willst: CHANGE_ROOT_FORCE=1 ./change_root.sh \"$NEW\")"
        exit 1
    fi
    echo "(CHANGE_ROOT_FORCE=1 gesetzt - fahre trotzdem fort)"
fi

if [ ! -f settings.json ]; then
    echo "FEHLER: settings.json nicht da"; exit 1
fi

OLD="$(python3 -c "import json; print(json.load(open('settings.json')).get('root','').strip())")"
echo "alter root (settings.json): ${OLD:-<leer>}"
echo "neuer root                : $NEW"
echo

# slot finden
SLOT=""
slot_count=0
for d in data/*/; do
    [ -f "$d/mediasearch.db" ] || continue
    slot_count=$((slot_count+1))
    LAST="$d"
done

if [ "$slot_count" -eq 0 ]; then
    echo "kein DB-slot unter data/ - nichts zu aendern."
    exit 1
elif [ "$slot_count" -eq 1 ]; then
    SLOT="$LAST"
    echo "einziger DB-slot gefunden: $SLOT"
else
    # mehrere slots: den nehmen dessen root.txt zum alten root passt
    for d in data/*/; do
        [ -f "$d/mediasearch.db" ] || continue
        [ -f "$d/root.txt" ] || continue
        rt="$(cat "$d/root.txt" 2>/dev/null | tr -d '[:space:]')"
        if [ -n "$OLD" ] && [ "$rt" = "$(echo "$OLD" | tr -d '[:space:]')" ]; then
            SLOT="$d"; break
        fi
    done
    if [ -z "$SLOT" ]; then
        echo "mehrere DB-slots vorhanden, aber keiner matcht den alten root."
        echo "manuell entscheiden:"
        for d in data/*/; do
            [ -f "$d/mediasearch.db" ] || continue
            echo "  $d  -> $(cat "$d/root.txt" 2>/dev/null | head -1)"
        done
        echo "dann root.txt im richtigen slot manuell aendern und"
        echo "settings.json 'root' anpassen."
        exit 1
    fi
    echo "passender DB-slot: $SLOT"
fi

# settings.json updaten (atomar via tempfile)
python3 - "$NEW" <<'PY'
import json, sys, pathlib
new = sys.argv[1]
p = pathlib.Path("settings.json")
s = json.load(open(p))
s["root"] = new
tmp = p.with_suffix(".json.tmp")
tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
tmp.replace(p)
print("settings.json aktualisiert")
PY

# root.txt updaten
echo "$NEW" > "$SLOT/root.txt"
echo "$SLOT/root.txt aktualisiert"

echo
echo "fertig. jetzt server neu starten:"
echo "  ./run.sh restart"
echo
echo "ACHTUNG: die in der DB gespeicherten Dateipfade sind RELATIV zum"
echo "root, also ohne das Verzeichnisprefix. Solange die relative"
echo "ordnerstruktur unter dem neuen root gleich ist, funktionieren alle"
echo "tags + thumbnails weiter ohne Re-Scan."
