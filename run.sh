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
            # eigenes Profil unter browser/profile/ -> portable
            mkdir -p "$HERE/browser/profile"
            "$browser" --profile "$HERE/browser/profile" --new-window "$url" \
                >/dev/null 2>&1 &
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
  ui              Web-UI starten (Default wenn kein Command angegeben)
                  Startet serve.py + oeffnet Browser (portable falls vorhanden)
                  Browser-Pfad in dieser Reihenfolge:
                    browser/firefox/firefox
                    browser/*.AppImage
                    System: firefox, chromium, google-chrome, brave-browser
                    xdg-open

  serve           Nur serve.py, kein Browser (z.B. fuer Headless / Cron)
                    --host 0.0.0.0          (oder via config.toml)
                    --port 8765
                  Read-Args: oeffnet ohne Root, falls in settings.json gesetzt

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
  ./run.sh tag /pfad/zu/medien --limit 500
  ./run.sh dedupe /pfad/zu/medien --only image
  ./run.sh thumbs /pfad/zu/medien --missing-only

Config:    config.toml (LLM, Server, Viewers, ...)
DB:        data/<root-hash>/mediasearch.db (lokal, portabel)
Logs:      mediasearch.log
EOF
}

case "${1:-ui}" in
    ui)             shift; cmd_ui "$@" ;;
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
