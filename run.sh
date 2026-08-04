#!/usr/bin/env bash
# Convenience launcher.
# - 'ui'    startet serve.py + oeffnet Browser (portable falls vorhanden)
# - 'serve' nur serve.py, kein Browser
# - 'tag'   tag.py CLI
# - 'thumbs' thumbs.py CLI
# - 'setup-browser' laedt portable Firefox nach browser/
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# venv setup
# Erkennt automatisch ein 'stale' venv: nach einem mv des
# mediasearch-ordners zeigen die shebangs in .venv/bin/python auf den
# alten pfad und schlagen mit 'No such file or directory' fehl. In dem
# fall: .venv wegwerfen und neu bauen.
venv_is_stale() {
    [ -d .venv ] || return 1
    # python im venv aufrufbar?
    .venv/bin/python -c '' 2>/dev/null && return 1
    return 0
}

if venv_is_stale; then
    echo "venv ist stale (vermutlich nach mediasearch-folder-move) - baue neu auf..."
    rm -rf .venv
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
    # Wenn vendor/ mit lokalen wheels vorhanden ist: offline installieren.
    # Sonst: normal von PyPI ziehen (braucht Internet).
    if [ -d vendor ] && compgen -G "vendor/*.whl" >/dev/null; then
        echo "vendor/ gefunden - installiere offline aus lokalen wheels..."
        .venv/bin/pip install --no-index --find-links vendor -r requirements.txt
    else
        .venv/bin/pip install -r requirements.txt
    fi
fi
# venv-Python IMMER per absolutem Pfad aufrufen - NICHT 'source activate' + 'python'.
# Grund (das wiederkehrende Problem): nach einem Ordner-Move zeigt der in
# .venv/bin/activate hartkodierte VIRTUAL_ENV-Pfad auf das ALTE Verzeichnis.
# Das prependet ein nicht-existentes dir an PATH, danach ist 'python' nicht
# auffindbar (viele Systeme haben nur 'python3') -> "exec python: nicht gefunden".
# .venv/bin/python funktioniert dagegen auch nach einem Move ohne Neubau.
PY="$HERE/.venv/bin/python"

read_port() {
    "$PY" -c "import tomllib; \
        print(tomllib.load(open('config.toml','rb'))['serve'].get('port', 8765))" \
        2>/dev/null || echo 8765
}

find_browser() {
    # 1) Portable Firefox in browser/firefox/
    if [ -x "$HERE/browser/firefox/firefox" ]; then
        echo "$HERE/browser/firefox/firefox"; return
    fi
    # 2) Chromium AppImage in browser/
    for f in "$HERE/browser/"*.AppImage; do
        [ -x "$f" ] && { echo "$f"; return; }
    done
    # 3) Lokales firefox / chromium / google-chrome
    for cand in firefox chromium chromium-browser google-chrome brave-browser; do
        if command -v "$cand" >/dev/null 2>&1; then
            command -v "$cand"; return
        fi
    done
    # 4) xdg-open
    if command -v xdg-open >/dev/null 2>&1; then
        echo "xdg-open"; return
    fi
    echo ""
}

launch_browser() {
    local url="$1"
    local browser; browser=$(find_browser)
    if [ -z "$browser" ]; then
        echo "kein Browser gefunden. Oeffne manuell: $url"
        return
    fi
    echo "Browser: $browser"
    case "$browser" in
        */firefox)
            mkdir -p "$HERE/browser/profile"
            "$browser" --profile "$HERE/browser/profile" --new-window "$url" \
                >/dev/null 2>&1 &
            ;;
        *Chromium*.AppImage|*chromium*.AppImage|*chrome*.AppImage|*Chrome*.AppImage)
            # Chromium-Engine: portables User-Data-Dir unter browser/chromium-data/
            # --no-sandbox als Fallback fuer Systeme ohne user namespaces
            # --appimage-extract-and-run umgeht fehlendes fuse2 (haeufig auf
            # neuen Distros - sonst startet die AppImage stumm und nichts kommt)
            # stdout/stderr nach browser/chromium.log fuer Debug
            mkdir -p "$HERE/browser/chromium-data"
            "$browser" --appimage-extract-and-run \
                --user-data-dir="$HERE/browser/chromium-data" \
                --no-sandbox --new-window "$url" \
                > "$HERE/browser/chromium.log" 2>&1 &
            echo "  Chromium-Logs: $HERE/browser/chromium.log"
            ;;
        *xdg-open)
            xdg-open "$url" >/dev/null 2>&1 &
            ;;
        *)
            "$browser" "$url" >/dev/null 2>&1 &
            ;;
    esac
}

cmd_ui() {
    # Optionales erstes Argument: medien-root. Wenn angegeben und anders als
    # in settings.json, wird einfach das AKTIVE Wurzelverzeichnis umgeschaltet.
    # Jeder Root hat seinen eigenen DB-Slot (data/<hash>) - die Tags des alten
    # Roots bleiben erhalten, Zurueckwechseln zeigt sie wieder. Fuer einen
    # echten Medien-UMZUG (DB an neuen Pfad mitnehmen) gibt es ./change_root.sh.
    if [ $# -ge 1 ] && [ "${1:0:1}" != "-" ]; then
        local newroot="$1"; shift
        local curroot=""
        if [ -f settings.json ]; then
            curroot="$(python3 -c "import json; \
                print(json.load(open('settings.json')).get('root','').strip())" \
                2>/dev/null || echo "")"
        fi
        # Vergleich tolerant gegen trailing slash
        if [ "${curroot%/}" != "${newroot%/}" ]; then
            echo "medien-root wird umgeschaltet:"
            echo "  alt: ${curroot:-<leer>}"
            echo "  neu: $newroot"
            # settings.json atomar updaten, andere Keys erhalten.
            python3 - "$newroot" <<'PY'
import json, pathlib, sys
new = sys.argv[1]
p = pathlib.Path("settings.json")
s = {}
if p.exists():
    try:
        s = json.load(open(p))
    except Exception:
        s = {}
s["root"] = new
tmp = p.with_suffix(".json.tmp")
tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
tmp.replace(p)
print(f"settings.json: root = {new}")
PY
        else
            echo "medien-root unveraendert ($curroot)."
        fi
    fi

    local port; port=$(read_port)
    local url="http://127.0.0.1:${port}"
    echo "==============================================="
    echo " mediasearch UI"
    echo "  Port:  ${port}"
    echo "  URL:   ${url}"
    echo "  Logs:  ${HERE}/mediasearch.log"
    echo "==============================================="

    # Liveness ueber /api/ping pruefen - superleicht, KEINE DB. (Frueher
    # /api/stats: auf grosser Bibliothek langsam -> -m 1 lief in die Timeout,
    # run.sh hielt den Server faelschlich fuer tot und startete eine zweite
    # Instanz -> Port-Konflikt -> gar kein Browser.)
    if curl -sf -m 3 "${url}/api/ping" >/dev/null 2>&1; then
        echo "serve.py laeuft bereits auf ${url} - oeffne nur den Browser."
        echo "(Falls du wirklich neu starten willst: './run.sh restart')"
        launch_browser "$url"
        exit 0
    fi

    # Sonst: starten und Browser aufmachen, sobald der Server antwortet.
    # Grosszuegiges Fenster (bis ~180s): der DB-Warmup laeuft zwar im
    # Hintergrund, aber uvicorn + erster Bind koennen auf grossen Libs kurz
    # dauern. Server ist ab dem ersten ping erreichbar, auch waehrend die DB
    # noch migriert.
    (
        opened=0
        for _ in $(seq 1 180); do
            if curl -sf -m 2 "${url}/api/ping" >/dev/null 2>&1; then
                launch_browser "$url"
                opened=1
                break
            fi
            sleep 1
        done
        if [ "$opened" -eq 0 ]; then
            echo "Browser-Opener: Server hat in 180s nicht geantwortet - oeffne nicht."
            echo "  Falls der Port belegt ist (haengender serve.py): './run.sh restart'"
        fi
    ) &

    echo "starte serve.py (Strg+C zum Beenden)"
    echo
    exec "$PY" serve.py "$@"
}

show_help() {
cat <<EOF
mediasearch - LLM-Vision-Tagging und -Suche fuer lokale Foto/Video-Sammlung

Aufruf:  ./run.sh <command> [opts]

Commands:
  ui [root]       Web-UI starten (Default wenn kein Command angegeben)
                  Startet serve.py + oeffnet Browser (portable falls vorhanden)
                  Optional: medien-root als argument. Weicht er vom wert in
                  settings.json ab -> das AKTIVE Wurzelverzeichnis wird
                  umgeschaltet (settings.json 'root'). Jeder Root hat seinen
                  eigenen DB-Slot; die Tags des alten bleiben erhalten,
                  Zurueckwechseln zeigt sie wieder. Beim naechsten Mal reicht
                  './run.sh ui'. Fuer einen echten Medien-UMZUG (DB an neuen
                  Pfad mitnehmen) -> ./change_root.sh.
                  Browser-Pfad in dieser Reihenfolge:
                    browser/firefox/firefox
                    browser/*.AppImage
                    System: firefox, chromium, google-chrome, brave-browser
                    xdg-open

  serve           Nur serve.py, kein Browser (z.B. fuer Headless / Cron)
                    --host 0.0.0.0          (oder via config.toml)
                    --port 8765
                  Read-Args: oeffnet ohne Root, falls in settings.json gesetzt

  restart         serve.py hart killen + neu starten + Browser oeffnen
                  Nuetzlich nach git pull oder wenn der Server haengt.

  tag <root>      LLM-Vision-Tagging via CLI (was die UI im Hintergrund macht)
                    --limit N               max Files in diesem Run
                    --workers N             parallele LLM-Calls
                    --retry-errors          Fehlerhafte Files nochmal probieren
                    --retag                 alle 'done'-Files neu taggen
                    --since-days N          Files der letzten N Tage (mtime)
                                            neu taggen (done/error -> pending)
                    --only image|video      nur ein Typ
                    --no-scan               Filesystem-Scan ueberspringen
                    --scan-only             nur Scan, kein LLM-Tagging
                    --endpoint URL          LLM-Endpoint (sonst aus config.toml)
                    --model NAME            LLM-Model (sonst aus config.toml)

  thumbs <root>   Thumbnails (240x240) neu erzeugen (ohne LLM)
                    --only image|video
                    --missing-only          nur fehlende Thumbs

  dedupe <root>   Doubletten-Hashes berechnen (BLAKE2b + Perceptual Hash)
                    --all                   alle Files neu hashen
                    --only image|video      nur ein Typ
                  Anschliessend im UI: Setup -> 'Doubletten' anklicken

  diag [root]     Diagnose: warum findet der Re-Scan neue Files nicht?
                  Vergleicht Dateisystem mit DB und deckt Symlink-Ordner,
                  nicht erkannte Endungen oder DB-/Root-Probleme auf.
                  Ohne Argument: Root aus settings.json.

  db-check   [root]          DB-Integritaet pruefen + Backups auflisten
  db-backup  [root]          manuelles DB-Backup (data/<slot>/backups/)
  db-repair  [root]          korrupte DB best-effort reparieren
                             (sichert die defekte DB vorher weg)
  db-restore [root] [datei]  aus Backup wiederherstellen (ohne datei: neuestes)
                  Auto-Backup laeuft nach jedem Tagging-Lauf und 1x/Tag beim
                  Server-Start; Rotation auf die letzten 7 Tages-Backups.

  setup-browser   Laedt einen portablen Browser nach browser/
                    (kein Argument)  -> Firefox-Tarball (~80 MB, GTK)
                    --chromium       -> ungoogled-chromium AppImage (~200 MB,
                                        oft fluessiger auf X11)
                  Sprache fuer Firefox via env: LANG_TAG=de (default) / en / ...

  help            diese Uebersicht

Beispiele:
  ./run.sh                                    # = ./run.sh ui
  ./run.sh ui /pfad/zu/bibliothek             # Root umschalten + start
  ./run.sh restart                            # serve.py hart neu starten
  ./run.sh tag /pfad/zu/medien --limit 500
  ./run.sh dedupe /pfad/zu/medien --only image
  ./run.sh thumbs /pfad/zu/medien --missing-only

Helper:
  ./change_root.sh /neuer/medien/pfad         # nach Medien-Move
  ./setup_browser.sh [--chromium]             # portablen Browser laden
  ./vendor_wheels.sh                          # wheels cachen fuer offline-rebuild

Hinweis venv:
  Stale .venv (nach mediasearch-folder-move) wird automatisch erkannt
  und neu gebaut. Wenn vendor/ wheels enthaelt, geschieht das offline -
  sonst braucht es Internet fuer pip.

Config:    config.toml (LLM, Server, Viewers, ...)
DB:        data/<root-hash>/mediasearch.db (lokal, portabel)
Logs:      mediasearch.log
EOF
}

cmd_restart() {
    echo "stoppe laufende serve.py-prozesse..."
    pkill -9 -f "serve\.py" || true
    sleep 1
    # port von eventuell haengengebliebener bindung loesen lassen
    local port; port=$(read_port)
    if command -v fuser >/dev/null; then
        fuser -k -n tcp "$port" 2>/dev/null || true
    fi
    sleep 1
    echo "starte neu..."
    cmd_ui
}

case "${1:-ui}" in
    ui)             shift; cmd_ui "$@" ;;
    restart)        shift; cmd_restart "$@" ;;
    serve)          shift; exec "$PY" serve.py  "$@" ;;
    tag)            shift; exec "$PY" tag.py    "$@" ;;
    thumbs)         shift; exec "$PY" thumbs.py "$@" ;;
    dedupe)         shift; exec "$PY" dedupe.py "$@" ;;
    diag)           shift; exec "$PY" diag_scan.py "$@" ;;
    db-check)       shift; exec "$PY" dbtools.py check   "$@" ;;
    db-backup)      shift; exec "$PY" dbtools.py backup  "$@" ;;
    db-repair)      shift; exec "$PY" dbtools.py repair  "$@" ;;
    db-restore)     shift; exec "$PY" dbtools.py restore "$@" ;;
    setup-browser)  shift; exec bash "$HERE/setup_browser.sh" "$@" ;;
    help|-h|--help) show_help ;;
    *)  echo "Unbekanntes Command: ${1}"
        echo
        show_help
        exit 1
        ;;
esac
