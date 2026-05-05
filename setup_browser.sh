#!/usr/bin/env bash
# Laedt einen portablen Browser nach <scripts>/browser/.
# Optionen:
#   ./setup_browser.sh             - Firefox (ca. 80 MB, GTK)
#   ./setup_browser.sh --chromium  - ungoogled-chromium AppImage (ca. 200 MB)
#                                    Auf X11 oft fluessiger im Compositing.
#
# Linux x86_64. Fuer Windows/Mac einfach manuell die portable-Version in
# <scripts>/browser/ ablegen - run.sh findet AppImages automatisch.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/browser"
LANG_TAG="${LANG_TAG:-de}"
mkdir -p "$DEST"

mode="firefox"
if [ "${1:-}" = "--chromium" ]; then mode="chromium"; fi

cd "$DEST"

if [ "$mode" = "firefox" ]; then
    if [ -x "firefox/firefox" ]; then
        echo "Firefox bereits installiert: $DEST/firefox/firefox"
        exit 0
    fi
    URL="https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64&lang=${LANG_TAG}"
    echo "lade Firefox-Tarball nach $DEST ..."
    curl -L --progress-bar -o firefox.tar.xz "$URL"
    echo "entpacke ..."
    tar xf firefox.tar.xz
    rm firefox.tar.xz
    echo
    echo "fertig: $DEST/firefox/firefox"
    echo "Profil portable unter: $DEST/profile/"
    exit 0
fi

# Chromium-Variante - AppImage von ungoogled-chromium-binaries
# Wir laden die "latest"-Liste und picken die neueste Linux-x64-AppImage.
APPIMAGE_PATTERN="ungoogled-chromium*x86_64.AppImage"
if compgen -G "$DEST/$APPIMAGE_PATTERN" >/dev/null; then
    echo "Chromium-AppImage bereits vorhanden:"
    ls -la "$DEST/"$APPIMAGE_PATTERN
    exit 0
fi

# Versuche, die latest-URL aus dem Releases-API zu holen
echo "suche aktuellste ungoogled-chromium AppImage ..."
LATEST_URL=$(curl -fsSL "https://api.github.com/repos/ungoogled-software/ungoogled-chromium-binaries/releases/latest" \
    | grep -oE 'https://[^"]+x86_64\.AppImage' | head -1 || true)

if [ -z "$LATEST_URL" ]; then
    echo "Konnte AppImage-URL nicht automatisch ermitteln."
    echo "Bitte manuell von einer der Quellen herunterladen und nach '$DEST/' ablegen:"
    echo "  - https://ungoogled-software.github.io/ungoogled-chromium-binaries/"
    echo "  - https://github.com/ungoogled-software/ungoogled-chromium-binaries/releases"
    echo "  - https://chromium.appimage.io/ (alternative Build)"
    exit 1
fi

echo "lade $LATEST_URL ..."
fname="$(basename "$LATEST_URL")"
curl -L --progress-bar -o "$fname" "$LATEST_URL"
chmod +x "$fname"
echo
echo "fertig: $DEST/$fname"
echo "run.sh erkennt jede *.AppImage in $DEST/ automatisch."
