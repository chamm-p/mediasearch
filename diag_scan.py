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
from common import DATA_DIR, db_path, encode_surrogates, media_type


def _raw_root() -> str | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    sp = Path(__file__).resolve().parent / "settings.json"
    if sp.is_file():
        try:
            r = (json.loads(sp.read_text(encoding="utf-8")).get("root") or "").strip()
            if r:
                return r
        except Exception:
            pass
    return None


def _slot_inventory(selected_db: Path) -> None:
    """Listet ALLE DB-Slots unter data/ mit root.txt + Zeilenzahl. Deckt auf,
    wenn nach Root-Umschalten mehrere Slots existieren und der Scan/die UI in
    einen anderen schreibt/liest als erwartet."""
    if not DATA_DIR.is_dir():
        print("  (kein data/-Verzeichnis)")
        return
    slots = sorted(d for d in DATA_DIR.iterdir()
                   if d.is_dir() and (d / common.DB_NAME).is_file())
    if not slots:
        print("  (keine DB-Slots gefunden)")
        return
    for d in slots:
        rt = ""
        rtp = d / "root.txt"
        if rtp.is_file():
            try:
                rt = rtp.read_text(encoding="utf-8").strip()
            except OSError:
                rt = "<unlesbar>"
        try:
            conn = sqlite3.connect(d / common.DB_NAME)
            n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
        except Exception:
            n = "?"
        mark = "  <-- AKTIV (Scan+UI schreiben/lesen hier)" \
            if (d / common.DB_NAME).resolve() == selected_db.resolve() else ""
        print(f"  {d.name}: {n} Files  root.txt={rt or '<leer>'}{mark}")


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
    raw = _raw_root()
    if raw is None:
        print("FEHLER: kein Root gefunden (settings.json ohne 'root' und kein Argument).")
        print("Aufruf: ./run.sh diag /pfad/zu/medien")
        sys.exit(1)
    # WICHTIG: exakt wie der Tagger/Server aufloesen (expanduser + resolve),
    # sonst liest diag evtl. einen ANDEREN DB-Slot als der Scan schreibt.
    root = Path(raw).expanduser().resolve()

    print(f"Root (settings-string): {raw}")
    print(f"Root (aufgeloest)     : {root}")
    if str(Path(raw).expanduser()) != str(root):
        print("  [i] settings-string und aufgeloester Pfad unterscheiden sich")
        print("      (Symlink/trailing slash/relativ) - das kann Slot-Verwirrung geben.")
    if not root.is_dir():
        print(f"\nFEHLER: Root ist kein Verzeichnis / nicht erreichbar: {root}")
        print("(Mount aktiv? Pfad korrekt? -> genau DAS koennte schon das Problem sein.)")
        sys.exit(1)

    dbp_sel = db_path(root)
    print(f"\nAktiver DB-Slot: {dbp_sel}")
    print("Alle DB-Slots unter data/:")
    _slot_inventory(dbp_sel)

    print("\nscanne Dateisystem (kann bei grossen Sammlungen kurz dauern)...\n")

    media_nolink, unrec = _walk_media(root, followlinks=False)
    media_link, _ = _walk_media(root, followlinks=True)
    n_no, n_yes = len(media_nolink), len(media_link)

    # DB-Stand (derselbe Slot wie oben ermittelt)
    dbp = dbp_sel
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

    # 3) sichtbar aber nicht in DB  -> genau der 114-Fall
    if missing_in_db:
        print(f"\n[!] {len(missing_in_db)} sichtbare Medien fehlen im AKTIVEN DB-Slot.")
        print("    Ein Scan gegen genau diesen Root/Slot WUERDE sie einfuegen. Beispiele:")
        for ex in list(missing_in_db)[:10]:
            print(f"      {ex}")
        print("\n    -> Direkt vom Terminal scannen+taggen (umgeht die UI-Rescan-Kette):")
        print(f'         ./run.sh tag "{root}"')
        print("       (fuegt die fehlenden als pending ein und taggt sie sofort).")
        print("    Falls die Zahl oben pro Slot verteilt ist: der Scan/die UI nutzt")
        print("    evtl. einen anderen Slot als gedacht (siehe Slot-Liste oben).")

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
