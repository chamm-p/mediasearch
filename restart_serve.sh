#!/usr/bin/env bash
# Hart restart von serve.py mit klarer Diagnose.
# Schreibt nach restart_serve.txt damit copy-paste klappt.
set +e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/restart_serve.txt"
PORT="${1:-8765}"

cd "$HERE"

{
  echo "==== restart_serve @ $(date) ===="
  echo "verzeichnis: $HERE"
  echo

  echo "--- vor kill ---"
  echo "alle serve.py-prozesse mit cwd:"
  for pid in $(pgrep -f "serve\.py"); do
      cwd="$(readlink /proc/$pid/cwd 2>/dev/null)"
      cmd="$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)"
      echo "  pid=$pid cwd=$cwd cmd=$cmd"
  done
  echo
  echo "port $PORT belegt von:"
  fuser -n tcp $PORT 2>&1
  echo

  echo "--- killing alles was 'serve.py' im cmdline hat ---"
  pkill -9 -f "serve\.py"
  sleep 1
  echo "nach kill:"
  pgrep -af "serve\.py" 2>&1 || echo "keine serve.py mehr"
  echo
  fuser -n tcp $PORT 2>&1 || true
  echo

  echo "--- pull + git log ---"
  git fetch origin 2>&1 | tail -5
  git reset --hard origin/main 2>&1 | tail -3
  git log --oneline -3
  echo

  echo "--- check lokale serve.py ---"
  grep -n "/api/dedupe/start" serve.py | head -3
  echo

  echo "--- starte serve.py im hintergrund ---"
  nohup ./run.sh serve > serve.log 2>&1 &
  newpid=$!
  echo "neue pid: $newpid"
  sleep 3
  echo "prozess-check:"
  ps -p $newpid -o pid,cmd 2>&1
  echo

  echo "--- routen-check ---"
  curl -s --max-time 3 "http://localhost:$PORT/openapi.json" \
      | tr ',' '\n' | grep -oE '"/api/[^"]*"' | sort -u | grep -E "dedupe|tagger|retag" \
      || echo "keine routen gefunden - server tot oder anderer port"
  echo
  echo "--- testaufruf ---"
  curl -s --max-time 3 -X POST "http://localhost:$PORT/api/dedupe/start" \
      -H "Content-Type: application/json" -d '{}' -w "\nHTTP %{http_code}\n"
  echo
  echo "==== ende - falls fehler: serve.log anschauen ===="
} > "$OUT" 2>&1

echo "fertig - inhalt von $OUT zeigen"
echo "(falls das nichts hilft: cat $HERE/serve.log)"
