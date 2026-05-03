# mediasearch

Lokale Foto- und Video-Sammlung mit LLM-Vision automatisch verschlagworten und
durchsuchen. Komplett portabel, eigene SQLite-DB, OpenAI-kompatibler
Vision-Endpoint deiner Wahl (vLLM, LM Studio, Ollama, etc.).

## Features

- **LLM-Vision-Tagging** ueber jeden OpenAI-kompatiblen Endpunkt
  (mehrere parallel, automatische Lastverteilung nach Geschwindigkeit)
- **FTS5-Volltextsuche** ueber Beschreibung + Tags + Pfad
- **Manuelle Tags** zusaetzlich zu Auto-Tags - bleiben beim Re-Tagging erhalten
- **Slideshow** im Browser (Random/Serial Toggle, Rotation, Vollbild,
  Tag-Buttons, Tastatur-Bedienung)
- **Random-Play** ueber `mpv` fuer gemischte Bilder/Videos
- **LoRA-Export** ausgewaehlter Bilder mit Tag-Sidecars
- **Inkrementell** - Re-Scan erkennt neue / verschobene / geloeschte Files,
  Tags ueberleben Renames
- **Synonyme** (configurable JSON) - Tag-Normalisierung ohne LLM-Aufruf
- **Kontext-Sidecars** pro Verzeichnis (`.mediasearch-context.txt`) als
  Disambiguierungs-Hilfe fuer das LLM
- **Portabel** - alles im Tool-Ordner, DB unter `data/<root-hash>/`

## Quickstart

### 1. Voraussetzungen

- Python 3.11+
- `ffmpeg` und `ffprobe` (fuer Video-Frame-Extraktion)
- `mpv` (fuer Random-Play)
- `xdg-open` (fuer Klick → Standard-Viewer)
- ein OpenAI-kompatibler Vision-Endpoint
  (z.B. vLLM mit `Qwen2.5-VL-7B-Instruct` oder LM Studio)

### 2. Installieren

```bash
git clone https://github.com/<user>/mediasearch.git
cd mediasearch

# Beispiel-Configs kopieren und anpassen
cp config.example.toml config.toml
cp synonyms.example.json synonyms.json
cp manual_tags.example.json manual_tags.json

# In config.toml den/die LLM-Endpoint(s) eintragen.
```

### 3. Optional: portabler Browser

```bash
./run.sh setup-browser   # laedt Firefox-Tarball nach browser/firefox/
```

### 4. Starten

```bash
./run.sh                 # = ./run.sh ui
```

Erstellt automatisch eine `.venv/`, installiert Pakete, startet den Server,
oeffnet den Browser auf `http://127.0.0.1:8765`.

Beim ersten Start: im Setup-Panel das **Wurzelverzeichnis** waehlen,
"Tagging starten" klicken. Optional `--limit 200` fuer einen Testlauf.

## Bedienung

### Hauptansicht
- Suche: Volltext ueber Tags + Beschreibung. Operatoren: `AND`, `OR`, `NOT`,
  `"phrase"`. Wildcards (Prefix `katz*`) funktionieren automatisch.
- Sortierung: Pfad/Name (Default), Neueste, Aelteste, Relevanz, Random
- Klick auf Bild → Slideshow ab dort. Klick auf Video → externer Player.
- Strg+Klick / Rechtsklick → Selektion. Selektion bleibt ueber Seiten.

### Slideshow-Hotkeys
| Taste | Aktion |
|---|---|
| Space / → | naechstes Bild |
| Backspace / ← | vorheriges Bild |
| T | Text/Tags-Overlay |
| H | Pfad-Balken hart aus/an |
| F | Vollbild |
| L | um 90° drehen |
| R | Random ↔ Serial |
| O | extern oeffnen |
| P | Auto-Advance Pause/Play |
| Esc | schliessen |

### CLI-Befehle
```bash
./run.sh ui                    # Web-UI + Browser starten
./run.sh serve                 # nur Server (kein Browser)
./run.sh tag <root> --limit N  # CLI-Tagging
./run.sh thumbs <root>         # Thumbnails neu erzeugen
./run.sh setup-browser         # portablen Browser laden
```

## Konfiguration

### `config.toml`
Defaults fuer LLM-Endpoint(s), Tag-Optionen, Server-Host/Port. Mehrere
Endpoints werden automatisch nach Geschwindigkeit verteilt.

### `synonyms.json`
Mappt z.B. `schiff/dampfer/yacht` → `boot`. Wird beim Tagging und bei der
Re-Normalisierung (UI-Button) angewendet.

### `manual_tags.json`
Definiert Gruppen von Tags fuer die manuelle Vergabe (z.B. `favorit`,
`familie`). Diese Tags werden beim LLM-Re-Tagging **nicht** ueberschrieben.

### `<verzeichnis>/.mediasearch-context.txt`
Pro-Ordner-Hinweis, der dem LLM als System-Message mitgegeben wird.
Hilft bei Disambiguierung (z.B. "Hochzeit Anna+Markus, 2024" → der LLM
tagged Torte als `hochzeitstorte` statt `geburtstagstorte`). Vererbt sich
auf Unterordner; naehere Datei gewinnt.

## Storage

Alles relativ zum Tool-Ordner — der Ordner ist portabel:
```
<scripts>/data/<root-hash>/mediasearch.db    # SQLite + FTS5
<scripts>/data/<root-hash>/thumbs/*.jpg      # Thumbnails (240x240, q=72)
<scripts>/data/<root-hash>/root.txt          # welcher Pfad zu welchem Hash
<scripts>/settings.json                      # UI-State
<scripts>/mediasearch.log                    # rotierte Logs
```

Mehrere Wurzelverzeichnisse koexistieren in `data/<hash>/`-Unterordnern
ohne Kollision.

## Sicherheit

Der Server hat **keine Authentifizierung**. `/api/open` und `/api/play`
starten Prozesse auf der Host-Maschine, `/file/{id}` liefert beliebige
Dateien aus dem Wurzelverzeichnis aus. Nur in vertrauenswuerdigen
Netzwerken auf `0.0.0.0` binden.

## Lizenz

MIT
