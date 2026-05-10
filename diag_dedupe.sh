#!/usr/bin/env bash
# Diagnose fuer den 'not found'-Fehler beim Hashes-berechnen-Button.
# Schreibt alles in diag_dedupe.txt - die Datei kannst du im
# Editor oeffnen und den Inhalt copy-pasten.
set +e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/diag_dedupe.txt"
PORT="${1:-8765}"

{
  echo "==== diag_dedupe @ $(date) ===="
  echo
  echo "--- pwd / git ---"
  echo "pwd: $HERE"
  cd "$HERE" && git log --oneline -5
  echo
  echo "grep dedupe-route in serve.py (lokale datei):"
  grep -n "/api/dedupe/start\|api_dedupe_start" serve.py 2>&1 | head -5
  echo

  echo "--- prozesse ---"
  echo "alle serve.py:"
  ps -ef | grep -E "serve\.py" | grep -v grep
  echo
  echo "alle tag.py / dedupe.py:"
  ps -ef | grep -E "tag\.py|dedupe\.py" | grep -v grep
  echo

  echo "--- port $PORT ---"
  if command -v ss >/dev/null; then
      ss -tlnp 2>/dev/null | grep ":$PORT "
  fi
  if command -v fuser >/dev/null; then
      echo "fuser:"
      fuser -n tcp $PORT 2>&1
  fi
  echo

  echo "--- routes des laufenden servers (via /openapi.json) ---"
  if command -v curl >/dev/null; then
      curl -s --max-time 3 "http://localhost:$PORT/openapi.json" \
          | tr ',' '\n' | grep -oE '"/api/[^"]*"' | sort -u
  else
      echo "curl nicht installiert"
  fi
  echo

  echo "--- testaufruf POST /api/dedupe/start ---"
  curl -s --max-time 3 -X POST "http://localhost:$PORT/api/dedupe/start" \
      -H "Content-Type: application/json" \
      -d '{}' -w "\nHTTP %{http_code}\n"
  echo
  echo "==== ende ===="
} > "$OUT" 2>&1

echo "fertig - bitte den inhalt von $OUT zeigen"
echo "  (im editor oeffnen, alles markieren, hier reinpasten)"
