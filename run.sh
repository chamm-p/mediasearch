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

    # Browser-Opener in Subshell: wartet bis Port antwortet, dann Browser auf
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

    # serve.py im Vordergrund - Python-Output und uvicorn-Logs direkt sichtbar
    echo "starte serve.py (Strg+C zum Beenden)"
    echo
    exec python serve.py "$@"
}

case "${1:-ui}" in
    ui)             shift; cmd_ui "$@" ;;
    serve)          shift; exec python serve.py  "$@" ;;
    tag)            shift; exec python tag.py    "$@" ;;
    thumbs)         shift; exec python thumbs.py "$@" ;;
    setup-browser)  exec bash "$HERE/setup_browser.sh" ;;
    *) echo "usage: $0 {ui|serve|tag|thumbs|setup-browser} [opts]"; exit 1 ;;
esac
