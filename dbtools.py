"""DB-Wartung: pruefen, sichern, reparieren, wiederherstellen.

Aufruf ueber run.sh:
  ./run.sh db-check   [root]              Integritaet pruefen + Backups auflisten
  ./run.sh db-backup  [root]              manuelles Backup (pro Tag eins)
  ./run.sh db-repair  [root]              korrupte DB best-effort reparieren
  ./run.sh db-restore [root] [backup.db]  aus Backup wiederherstellen
                                          (ohne Datei: neuestes Backup)

Backups liegen unter data/<slot>/backups/. Automatisch wird nach jedem
Tagging-Lauf und einmal pro Tag beim Server-Start gesichert.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import common
from common import (backup_db, backups_dir, db_path, init_db, integrity_check,
                    list_backups, optimize_db)

HERE = Path(__file__).resolve().parent


def _root() -> Path:
    raw = None
    # 2. Argument ist bei manchen Kommandos der root; wir suchen den ersten,
    # der KEINE .db-Datei ist (die ist bei restore das Backup-Argument).
    for a in sys.argv[2:]:
        if a and not a.endswith(".db"):
            raw = a
            break
    if raw is None:
        sp = HERE / "settings.json"
        if sp.is_file():
            try:
                raw = (json.loads(sp.read_text(encoding="utf-8")).get("root") or "").strip() or None
            except Exception:
                raw = None
    if not raw:
        print("FEHLER: kein Root (settings.json ohne 'root' und kein Argument).")
        sys.exit(1)
    return Path(raw).expanduser().resolve()


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def cmd_check(root: Path) -> None:
    p = db_path(root)
    print(f"DB: {p}")
    ok, detail = integrity_check(root)
    print(f"Integritaet: {'OK' if ok else 'DEFEKT'}  ({detail})")
    bks = list_backups(root)
    print(f"\nBackups ({len(bks)}) unter {backups_dir(root)}:")
    for b in bks[-10:]:
        mb = b.stat().st_size / 1e6
        print(f"  {b.name}  ({mb:.1f} MB)")
    if not ok:
        print("\n-> Reparieren:  ./run.sh db-repair")
        print("   oder aus Backup:  ./run.sh db-restore")


def cmd_backup(root: Path) -> None:
    ok, _ = integrity_check(root)
    if not ok:
        print("WARNUNG: DB ist defekt - Backup einer defekten DB ist wenig wert.")
        print("Besser zuerst ./run.sh db-repair. Sichere trotzdem als 'corrupt-'...")
        src = db_path(root)
        dst = backups_dir(root) / f"corrupt-{_stamp()}.db"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"Roh-Kopie: {dst}")
        return
    dst = backup_db(root, daily_dedup=False)   # manuell -> immer neues Backup
    print(f"Backup erstellt: {dst}")


def _raw_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def cmd_repair(root: Path) -> None:
    src = db_path(root)
    if not src.is_file():
        print(f"FEHLER: keine DB unter {src}")
        sys.exit(1)
    bdir = backups_dir(root)
    bdir.mkdir(parents=True, exist_ok=True)

    ok, detail = integrity_check(root)
    print(f"DB: {src}\nIntegritaet vorher: {'OK' if ok else 'DEFEKT'} ({detail})")

    # Immer zuerst die aktuelle (evtl. defekte) DB roh sichern.
    safety = bdir / f"corrupt-{_stamp()}.db"
    _raw_copy(src, safety)
    print(f"Aktuelle DB gesichert nach: {safety}")

    if ok:
        print("DB ist intakt - baue nur FTS/Index neu + VACUUM...")
        try:
            conn = common.connect(root)
            try:
                conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
                conn.commit()
            finally:
                conn.close()
            optimize_db(root)
        except Exception as e:
            print(f"  (optimize/rebuild-Hinweis: {e})")
        print("Fertig. (War nicht korrupt - falls das Problem bleibt: db-restore.)")
        return

    recovered = src.with_name("mediasearch.recovered.db")
    if recovered.exists():
        recovered.unlink()

    used = None
    # 1) Bevorzugt: sqlite3-CLI '.recover' (rettet am meisten aus korrupten DBs)
    if shutil.which("sqlite3"):
        print("versuche Recovery via sqlite3 .recover ...")
        try:
            p1 = subprocess.Popen(["sqlite3", str(src), ".recover"],
                                  stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["sqlite3", str(recovered)],
                                  stdin=p1.stdout)
            p1.stdout.close()
            p2.communicate(timeout=1800)
            if p2.returncode == 0 and recovered.is_file():
                used = "sqlite3 .recover"
        except Exception as e:
            print(f"  .recover fehlgeschlagen: {e}")

    # 2) Fallback: Python-Salvage - Zeilen tolerant kopieren
    if used is None:
        print("Fallback: rette Zeilen zeilenweise nach neuer DB ...")
        try:
            _python_salvage(src, recovered)
            used = "python-salvage"
        except Exception as e:
            print(f"FEHLER bei Salvage: {e}")

    if used is None or not recovered.is_file():
        print("\nReparatur nicht moeglich. Nutze ein Backup:")
        print("  ./run.sh db-restore")
        sys.exit(1)

    # Recovered verifizieren
    try:
        c = sqlite3.connect(recovered)
        n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chk = c.execute("PRAGMA integrity_check").fetchone()[0]
        c.close()
    except Exception as e:
        print(f"recovered-DB nicht lesbar ({e}) - nutze ein Backup: ./run.sh db-restore")
        sys.exit(1)
    print(f"recovered via {used}: {n} Files, integrity={chk}")

    # Einspielen: alte weg (schon gesichert), recovered -> db_path
    for ext in ("", "-wal", "-shm"):
        s = Path(str(src) + ext)
        if s.exists():
            try: s.unlink()
            except OSError: pass
    recovered.replace(src)
    # Schema/Indizes/FTS sicherstellen
    common._INIT_DONE.discard(str(root))
    init_db(root, force=True)
    ok2, det2 = integrity_check(root)
    print(f"Integritaet nachher: {'OK' if ok2 else 'DEFEKT'} ({det2})")
    print(f"Fertig. {n} Files gerettet. (Defekte Original-DB: {safety})")


def _python_salvage(src: Path, dst: Path) -> None:
    """Kopiert die files-Tabelle tolerant in eine frische DB (SCHEMA).
    Ueberspringt unlesbare Bereiche best-effort."""
    newconn = sqlite3.connect(dst)
    newconn.executescript(common.SCHEMA)
    oldconn = sqlite3.connect(src)
    cols = "id, rel_path, type, size, mtime, description, tags, manual_tags, " \
           "status, error, tagged_at, seen_at, started_at, content_hash, phash_int"
    copied = 0
    try:
        cur = oldconn.execute(f"SELECT {cols} FROM files")
        while True:
            try:
                row = cur.fetchone()
            except Exception:
                break   # korrupte Stelle -> Rest nicht mehr lesbar
            if row is None:
                break
            try:
                newconn.execute(
                    f"INSERT OR IGNORE INTO files({cols}) VALUES "
                    f"({','.join('?'*15)})", row)
                copied += 1
            except Exception:
                continue
        newconn.commit()
    finally:
        oldconn.close()
        newconn.close()
    print(f"  {copied} Zeilen gerettet")


def cmd_restore(root: Path) -> None:
    # Backup-Datei = Argument mit .db, sonst neuestes Backup
    chosen = None
    for a in sys.argv[2:]:
        if a.endswith(".db"):
            chosen = Path(a).expanduser()
            break
    if chosen is None:
        bks = list_backups(root)
        if not bks:
            print("Kein Backup vorhanden."); sys.exit(1)
        chosen = bks[-1]
    if not chosen.is_file():
        print(f"Backup nicht gefunden: {chosen}"); sys.exit(1)

    # Backup vorher pruefen
    try:
        c = sqlite3.connect(chosen)
        n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chk = c.execute("PRAGMA integrity_check").fetchone()[0]
        c.close()
    except Exception as e:
        print(f"Backup ist nicht lesbar ({e}) - anderes Backup waehlen.")
        sys.exit(1)

    src = db_path(root)
    # aktuelle DB sicherheitshalber wegsichern
    if src.is_file():
        safety = backups_dir(root) / f"before-restore-{_stamp()}.db"
        _raw_copy(src, safety)
        print(f"Aktuelle DB gesichert: {safety}")
    for ext in ("-wal", "-shm"):
        s = Path(str(src) + ext)
        if s.exists():
            try: s.unlink()
            except OSError: pass
    shutil.copy2(chosen, src)
    print(f"Wiederhergestellt aus {chosen.name}: {n} Files (integrity={chk}).")
    print("-> ./run.sh restart, dann pruefen. Ggf. './run.sh tag <root>' fuer Neues.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    root = _root()
    if cmd == "check":     cmd_check(root)
    elif cmd == "backup":  cmd_backup(root)
    elif cmd == "repair":  cmd_repair(root)
    elif cmd == "restore": cmd_restore(root)
    elif cmd == "list":    cmd_check(root)
    else:
        print(f"unbekanntes db-Kommando: {cmd}")
        print("erlaubt: check | backup | repair | restore | list")
        sys.exit(1)


if __name__ == "__main__":
    main()
