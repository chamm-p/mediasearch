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
if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi
source .venv/bin/activate

read_port() {
    .venv/bin/python -c "import tomllib; \
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
    # Optionales erstes Argument: medien-root. Wenn angegeben und anders
    # als in settings.json -> change_root.sh aufrufen, damit settings.json
    # + data/<slot>/root.txt synchron bleiben. Beim naechsten mal reicht
    # './run.sh ui'.
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
            echo "medien-root aendert sich:"
            echo "  alt: ${curroot:-<leer>}"
            echo "  neu: $newroot"
            if [ -x ./change_root.sh ]; then
                ./change_root.sh "$newroot"
            else
                echo "FEHLER: ./change_root.sh nicht ausfuehrbar"
                exit 1
            fi
        else
            echo "medien-root unveraendert ($curroot) - kein change_root noetig."
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

    # Schon eine Instanz auf dem Port? Dann nur Browser oeffnen, keine zweite
    # serve.py starten (Tagger laeuft ggf. dort weiter).
    if curl -sf -m 1 "${url}/api/stats" >/dev/null 2>&1; then
        echo "serve.py laeuft bereits auf ${url} - oeffne nur den Browser."
        echo "(Falls du wirklich neu starten willst: 'pkill -f serve.py' und nochmal)"
        launch_browser "$url"
        exit 0
    fi

    # Sonst: starten und Browser nach kurzer Wartezeit aufmachen
    (
        for _ in $(seq 1 60); do
            if curl -sf -m 1 "${url}/api/stats" >/dev/null 2>&1; then
                launch_browser "$url"
                exit 0
            fi
            sleep 0.5
        done
        echo "Browser-Opener: Port hat in 30s nicht geantwortet, oeffne nicht."
    ) &

    echo "starte serve.py (Strg+C zum Beenden)"
    echo
    exec python serve.py "$@"
}

show_help() {
cat <<EOF
mediasearch - LLM-Vision-Tagging und -Suche fuer lokale Foto/Video-Sammlung

Aufruf:  ./run.sh <command> [opts]

Commands:
  ui [root]       Web-UI starten (Default wenn kein Command angegeben)
                  Startet serve.py + oeffnet Browser (portable falls vorhanden)
                  Optional: medien-root als argument. Weicht er vom wert in
                  settings.json ab -> change_root laeuft automatisch
                  (settings.json + data/<slot>/root.txt werden umgebogen).
                  Beim naechsten Mal reicht dann './run.sh ui'.
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

  setup-browser   Laedt einen portablen Browser nach browser/
                    (kein Argument)  -> Firefox-Tarball (~80 MB, GTK)
                    --chromium       -> ungoogled-chromium AppImage (~200 MB,
                                        oft fluessiger auf X11)
                  Sprache fuer Firefox via env: LANG_TAG=de (default) / en / ...

  help            diese Uebersicht

Beispiele:
  ./run.sh                                    # = ./run.sh ui
  ./run.sh ui /neuer/medien/pfad              # einmaliger move + start
  ./run.sh restart                            # serve.py hart neu starten
  ./run.sh tag /pfad/zu/medien --limit 500
  ./run.sh dedupe /pfad/zu/medien --only image
  ./run.sh thumbs /pfad/zu/medien --missing-only

Helper:
  ./change_root.sh /neuer/medien/pfad         # nach Medien-Move
  ./setup_browser.sh [--chromium]             # portablen Browser laden

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
    serve)          shift; exec python serve.py  "$@" ;;
    tag)            shift; exec python tag.py    "$@" ;;
    thumbs)         shift; exec python thumbs.py "$@" ;;
    dedupe)         shift; exec python dedupe.py "$@" ;;
    setup-browser)  shift; exec bash "$HERE/setup_browser.sh" "$@" ;;
    help|-h|--help) show_help ;;
    *)  echo "Unbekanntes Command: ${1}"
        echo
        show_help
        exit 1
        ;;
esac
