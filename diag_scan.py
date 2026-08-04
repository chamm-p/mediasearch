"""Diagnose: warum findet der Re-Scan meine neuen Files nicht?

Vergleicht Dateisystem (genau die Logik des Taggers) mit der DB und deckt die
drei haeufigsten Ursachen auf:
  1. neue Files in SYMLINK-Unterordnern (os.walk ueberspringt die per Default)
  2. nicht erkannte Datei-ENDUNG (Format nicht in IMAGE_EXTS/VIDEO_EXTS)
  3. Files sind schon in der DB (dann ist "nichts neues" korrekt)

Aufruf:  ./run.sh diag        (nutzt den Root aus settings.json)
         ./run.sh diag /pfad  (expliziter Root)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import common
from common import db_path, encode_surrogates, media_type


def _root_from_args_or_settings() -> Path | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return Path(sys.argv[1]).expanduser()
    sp = Path(__file__).resolve().parent / "settings.json"
    if sp.is_file():
        try:
            r = (json.loads(sp.read_text(encoding="utf-8")).get("root") or "").strip()
            if r:
                return Path(r).expanduser()
        except Exception:
            pass
    return None


def _walk_media(root: Path, followlinks: bool) -> tuple[set[str], Counter]:
    """Liefert (set der media-rel-paths, Counter nicht-erkannter Endungen).
    Mit Zyklen-Schutz beim Symlink-Folgen (sonst Endlosschleife)."""
    media: set[str] = set()
    unrec: Counter = Counter()
    seen_real: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=followlinks):
        if followlinks:
            # bereits besuchte (aufgeloeste) Verzeichnisse nicht erneut betreten
            pruned = []
            for d in dirnames:
                rp = os.path.realpath(os.path.join(dirpath, d))
                if rp in seen_real:
                    continue
                seen_real.add(rp)
                pruned.append(d)
            dirnames[:] = pruned
        for name in filenames:
            p = Path(dirpath) / name
            if media_type(p) is not None:
                try:
                    media.add(encode_surrogates(str(p.relative_to(root))))
                except ValueError:
                    media.add(encode_surrogates(name))
            else:
                unrec[p.suffix.lower()] += 1
    return media, unrec


def main() -> None:
    root = _root_from_args_or_settings()
    if root is None:
        print("FEHLER: kein Root gefunden (settings.json ohne 'root' und kein Argument).")
        print("Aufruf: ./run.sh diag /pfad/zu/medien")
        sys.exit(1)
    if not root.is_dir():
        print(f"FEHLER: Root ist kein Verzeichnis / nicht erreichbar: {root}")
        print("(Mount aktiv? Pfad korrekt? -> genau DAS koennte schon das Problem sein.)")
        sys.exit(1)

    print(f"Root: {root}")
    print("scanne Dateisystem (kann bei grossen Sammlungen kurz dauern)...\n")

    media_nolink, unrec = _walk_media(root, followlinks=False)
    media_link, _ = _walk_media(root, followlinks=True)
    n_no, n_yes = len(media_nolink), len(media_link)

    # DB-Stand
    dbp = db_path(root)
    db_rel: set[str] = set()
    status_counts: dict[str, int] = {}
    if dbp.is_file():
        conn = sqlite3.connect(dbp)
        try:
            db_rel = {r[0] for r in conn.execute("SELECT rel_path FROM files")}
            for st, c in conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status"):
                status_counts[st] = c
        finally:
            conn.close()
    n_db = len(db_rel)

    symlink_hidden = media_link - media_nolink       # nur ueber Symlinks erreichbar
    missing_in_db = media_nolink - db_rel            # sichtbar, aber nicht in DB

    print("=" * 60)
    print(f"Medien sichtbar (ohne Symlink-Ordner): {n_no}")
    print(f"Medien sichtbar (MIT Symlink-Ordnern): {n_yes}")
    print(f"in DB ({dbp.name}):                    {n_db}")
    if status_counts:
        print("  DB-Status:", ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())))
    print("=" * 60)

    # 1) Symlink-Ordner
    if symlink_hidden:
        print(f"\n[!] {len(symlink_hidden)} Medien liegen NUR in SYMLINK-Unterordnern")
        print("    -> os.walk ueberspringt die per Default. Beispiele:")
        for ex in list(symlink_hidden)[:10]:
            print(f"      {ex}")
        print("    Fix: followlinks aktivieren (sag mir Bescheid, ich baue es ein).")

    # 2) nicht erkannte Endungen
    if unrec:
        print("\nNicht erkannte Datei-Endungen (top 15) - falls dein neues Format dabei ist,")
        print("wird es uebersprungen und muss zu IMAGE_EXTS/VIDEO_EXTS ergaenzt werden:")
        for ext, c in unrec.most_common(15):
            print(f"      {ext or '(ohne endung)'}: {c}")

    # 3) sichtbar aber nicht in DB
    if missing_in_db:
        print(f"\n[!] {len(missing_in_db)} sichtbare Medien fehlen in der DB.")
        print("    Ein Re-Scan SOLLTE die finden. Wenn nicht: lief der Scan evtl. gegen")
        print("    einen anderen Root, oder es ist ein Rechte-/Encoding-Problem. Beispiele:")
        for ex in list(missing_in_db)[:10]:
            print(f"      {ex}")

    # Unverarbeitete Files (haeufigster Fall: abgebrochener Lauf)
    n_pending    = status_counts.get("pending", 0)
    n_processing = status_counts.get("processing", 0)
    n_error      = status_counts.get("error", 0)
    n_todo = n_pending + n_processing + n_error

    # Verdikt
    print("\n" + "-" * 60)
    if n_todo > 0 and not missing_in_db and not symlink_hidden:
        print(f"VERDIKT: {n_todo} Files sind in der DB, aber noch NICHT getaggt")
        print(f"  (pending={n_pending}, processing={n_processing}, error={n_error}).")
        print("  'Nichts neues' beim Re-Scan ist korrekt - sie sind ja schon erfasst.")
        print("  Was tun:")
        if n_processing > 0:
            print("   1. './run.sh restart' - setzt haengende 'processing' (vom Abbruch)")
            print("      automatisch zurueck auf 'pending'.")
        print("   2. Im UI 'Tagging starten' (oder CLI './run.sh tag <root>').")
        print("      Der Lauf scannt kurz (findet nichts neues) und taggt dann alle")
        print("      pending-Files.")
        if n_error > 0:
            print(f"   3. Fuer die {n_error} Fehler-Files: Haken 'Retry errors' setzen")
            print("      (bzw. './run.sh tag <root> --retry-errors').")
    elif not symlink_hidden and not missing_in_db:
        print("VERDIKT: Alle sichtbaren Medien sind in der DB und getaggt (done).")
        print("  'Nichts neues' ist korrekt. Deine 'neuen' Files haben evtl. eine nicht")
        print("  erkannte Endung (siehe oben) ODER liegen ausserhalb dieses Roots.")
    elif symlink_hidden and not missing_in_db:
        print("VERDIKT: Deine neuen Files stecken in SYMLINK-Unterordnern (Ursache gefunden).")
    elif missing_in_db:
        print("VERDIKT: Es fehlen sichtbare Medien in der DB - echtes Scan-/Root-Problem.")
    if symlink_hidden or missing_in_db or (unrec and n_todo == 0):
        print("Schick mir diese Ausgabe (oder ein Foto davon), dann baue ich den Fix.")


if __name__ == "__main__":
    main()
