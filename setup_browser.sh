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
    if [ ! -x "firefox/firefox" ]; then
        URL="https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64&lang=${LANG_TAG}"
        echo "lade Firefox-Tarball nach $DEST ..."
        curl -L --progress-bar -o firefox.tar.xz "$URL"
        echo "entpacke ..."
        tar xf firefox.tar.xz
        rm firefox.tar.xz
    else
        echo "Firefox bereits installiert: $DEST/firefox/firefox"
    fi

    # Profil-Verzeichnis + user.js mit Performance-Tweaks anlegen
    # (WebRender / Hardware-Compositor an -> druckt Xorg-CPU spuerbar)
    mkdir -p "$DEST/profile"
    cat > "$DEST/profile/user.js" <<'PREFS'
// mediasearch Firefox-Tweaks fuer Xorg-Performance.
// Wird bei jedem Start gelesen und ueberschreibt prefs.js.

// WebRender (GPU-Compositor) hart einschalten - kein Software-Rasterizing
user_pref("gfx.webrender.all", true);
user_pref("gfx.webrender.compositor", true);
user_pref("gfx.webrender.compositor.force-enabled", true);

// Hardware-Beschleunigung
user_pref("layers.acceleration.force-enabled", true);
user_pref("media.hardware-video-decoding.force-enabled", true);

// EGL statt GLX auf X11 - oft fluessiger
user_pref("gfx.x11-egl.force-enabled", true);

// Animation-Reduktion (kein App-spezifischer Effekt, aber System-leichter)
user_pref("ui.prefersReducedMotion", 0);   // 0 = Animationen erlauben

// First-run / Start ohne Stoerung
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.firstRunURL", "");
PREFS
    echo
    echo "fertig: $DEST/firefox/firefox"
    echo "Profil portable + WebRender aktiviert: $DEST/profile/"
    exit 0
fi

# Chromium-Variante - AppImage von ivan-hc/Chromium-Web-Browser-appimage
# Dort liegen taegliche Builds aller Channels (stable/beta/candidate/edge).
# Wir nehmen "stable".
APPIMAGE_PATTERN="Chromium-*-x86_64.AppImage"
if compgen -G "$DEST/$APPIMAGE_PATTERN" >/dev/null; then
    echo "Chromium-AppImage bereits vorhanden:"
    ls -la "$DEST/"$APPIMAGE_PATTERN
    exit 0
fi

REPO="ivan-hc/Chromium-Web-Browser-appimage"
CHANNEL="${CHROMIUM_CHANNEL:-stable}"   # stable / candidate / beta / edge

echo "suche aktuellste Chromium-${CHANNEL} AppImage ..."
# Die neueste Release-Tag mit Assets finden (continuous-Tag hat keine Assets)
URL=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases?per_page=10" \
      | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+"' \
      | grep -oE 'https://[^"]+' \
      | grep -E "Chromium-${CHANNEL}-[^/]*-x86_64\.AppImage$" \
      | head -1 || true)

if [ -z "$URL" ]; then
    echo "Konnte AppImage-URL nicht automatisch ermitteln."
    echo "Manuelle Quellen:"
    echo "  - https://github.com/${REPO}/releases"
    echo "  - https://www.chromium.org/getting-involved/download-chromium/"
    echo "Datei nach '$DEST/' ablegen, run.sh erkennt sie."
    exit 1
fi

fname="$(basename "$URL")"
echo "lade $URL"
curl -L --progress-bar -o "$fname" "$URL"
chmod +x "$fname"

# Sandbox-Hinweis: AppImage-Chromium braucht entweder --no-sandbox oder
# user-namespaces. Wenn das System sie blockt, hilft --no-sandbox.
echo
echo "fertig: $DEST/$fname"
echo "run.sh erkennt jede *.AppImage in $DEST/ automatisch."
echo
echo "Hinweis: falls beim Start ein Sandbox-Fehler kommt, kann der AppImage"
echo "mit '--no-sandbox' gestartet werden. run.sh tut das automatisch fuer"
echo "Chromium-AppImages."
