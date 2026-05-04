"""Web-UI: Setup, Tagging-Steuerung, Suche, Slideshow, Random Play."""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from common import (DATA_DIR, connect, db_path, decode_surrogates, init_db,
                    load_config, load_manual_tags, load_synonyms,
                    natural_key, normalize_tags, optimize_db, thumb_path)


def _safe(s: str | None) -> str:
    """Macht beliebige Strings JSON-tauglich (lone surrogates -> ?)."""
    if not s:
        return s or ""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", errors="replace").decode("utf-8")

HERE = Path(__file__).resolve().parent
SETTINGS_PATH = HERE / "settings.json"
LOG_PATH = HERE / "mediasearch.log"

# ---------- logging ----------
logger = logging.getLogger("mediasearch")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_sh = logging.StreamHandler(sys.stderr); _sh.setFormatter(_fmt); logger.addHandler(_sh)
_fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3,
                          encoding="utf-8", errors="replace")
_fh.setFormatter(_fmt); logger.addHandler(_fh)
logger.propagate = False

class SafeJSONResponse(JSONResponse):
    """JSON-Response, die mit lone surrogates und sonstigen invalid-utf8-
    Zeichen in strings umgeht. Notwendig fuer Filenames mit defekter
    Encoding (cp1252-on-utf8 etc.)."""
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")


app = FastAPI(title="mediasearch", default_response_class=SafeJSONResponse)

# ---------- settings ----------

def default_settings() -> dict[str, Any]:
    cfg = load_config()
    return {
        "root":         "",
        "endpoint":     cfg.get("llm", {}).get("endpoint", "http://localhost:8000/v1"),
        "model":        cfg.get("llm", {}).get("model", "qwen-vl"),
        "api_key":      cfg.get("llm", {}).get("api_key", ""),
        "workers":      int(cfg.get("tag", {}).get("workers", 4)),
        "limit":        cfg.get("tag", {}).get("limit"),
        "retry_errors": bool(cfg.get("tag", {}).get("retry_errors", False)),
        "retag":        False,
        "only":         "",
    }


def _exc_msg(e: BaseException) -> str:
    """Make a non-empty, useful error string from any exception."""
    msg = str(e) or repr(e)
    name = type(e).__name__
    out = f"{name}: {msg}".strip()
    # OpenAI-spezifische Felder, falls vorhanden
    for attr in ("status_code", "code", "param", "type"):
        v = getattr(e, attr, None)
        if v not in (None, ""):
            out += f" [{attr}={v}]"
    body = getattr(e, "body", None) or getattr(e, "response", None)
    if body is not None:
        try:
            txt = body.text if hasattr(body, "text") else str(body)
            if txt and txt[:200] not in out:
                out += f" | body={txt[:200]}"
        except Exception:
            pass
    return out[:600] or f"{name}: <leere Fehlermeldung>"


PREFLIGHT_MODELS_TIMEOUT = 10.0
PREFLIGHT_CHAT_TIMEOUT   = 180.0   # erster Vision-Call kann >60s dauern (cold start)


def llm_preflight(endpoint: str, model: str, api_key: str) -> dict:
    """Schnelle Pruefung: erreichbar? Model bekannt? Mini-Chat moeglich?
    Returns {"ok": bool, "error": str|None, "available_models": list,
             "sample": str|None, "timings": {...}}."""
    t_total = time.time()
    timings: dict[str, float] = {}
    logger.info("llm_preflight start: endpoint=%s model=%s key=%s",
                endpoint or "<leer>", model or "<leer>",
                "set" if api_key else "<leer>")
    if not endpoint:
        return {"ok": False, "error": "endpoint leer", "available_models": [],
                "sample": None, "timings": timings}
    if not model:
        return {"ok": False, "error": "model leer", "available_models": [],
                "sample": None, "timings": timings}
    try:
        from openai import OpenAI
    except Exception as e:
        msg = f"openai SDK Import fehlgeschlagen: {_exc_msg(e)}"
        logger.error(msg)
        return {"ok": False, "error": msg, "available_models": [],
                "sample": None, "timings": timings}
    try:
        client = OpenAI(base_url=endpoint, api_key=api_key or "EMPTY",
                        timeout=PREFLIGHT_CHAT_TIMEOUT)
    except Exception as e:
        msg = f"OpenAI-Client Konstruktion fehlgeschlagen: {_exc_msg(e)}"
        logger.error(msg)
        return {"ok": False, "error": msg, "available_models": [],
                "sample": None, "timings": timings}

    available: list[str] = []
    models_err: str | None = None
    t0 = time.time()
    logger.info("llm_preflight: GET /models (timeout=%.0fs)", PREFLIGHT_MODELS_TIMEOUT)
    try:
        ml = client.with_options(timeout=PREFLIGHT_MODELS_TIMEOUT).models.list()
        if hasattr(ml, "data"):
            available = [m.id for m in ml.data][:50]
        timings["models_s"] = round(time.time() - t0, 2)
        logger.info("llm_preflight: /models ok in %.2fs (%d eintraege: %s)",
                    timings["models_s"], len(available),
                    ", ".join(available[:5]))
    except Exception as e:
        models_err = _exc_msg(e)
        timings["models_s"] = round(time.time() - t0, 2)
        logger.warning("llm_preflight: /models fehlgeschlagen nach %.2fs: %s",
                       timings["models_s"], models_err)

    t0 = time.time()
    logger.info("llm_preflight: POST chat/completions model=%s (timeout=%.0fs) "
                "- 1. Aufruf kann durch Modell-Warmup laenger dauern",
                model, PREFLIGHT_CHAT_TIMEOUT)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=4, temperature=0,
        )
        timings["chat_s"] = round(time.time() - t0, 2)
        timings["total_s"] = round(time.time() - t_total, 2)
        sample = (resp.choices[0].message.content or "")[:80]
        logger.info("llm_preflight: chat ok in %.2fs, sample=%r",
                    timings["chat_s"], sample)
        return {"ok": True, "error": None, "available_models": available,
                "sample": sample, "timings": timings}
    except Exception as e:
        timings["chat_s"] = round(time.time() - t0, 2)
        timings["total_s"] = round(time.time() - t_total, 2)
        msg = _exc_msg(e)
        logger.error("llm_preflight: chat fehlgeschlagen nach %.2fs: %s",
                     timings["chat_s"], msg)
        logger.debug("traceback:\n%s", traceback.format_exc())
        if available and model not in available:
            msg += f" | model '{model}' nicht in /models. Verfuegbar: {', '.join(available[:8])}"
        elif not available and models_err:
            msg += f" | /models auch nicht erreichbar: {models_err}"
        return {"ok": False, "error": msg, "available_models": available,
                "sample": None, "timings": timings}


def all_endpoints() -> list[dict]:
    """Liefert ALLE konfigurierten Endpoints (mit enabled-Flag)."""
    s = load_settings()
    cfg = load_config().get("llm", {})
    disabled = set(s.get("disabled_endpoints", []))
    multi_cfg = cfg.get("endpoints")
    if isinstance(multi_cfg, list) and multi_cfg:
        out = []
        for i, ep in enumerate(multi_cfg):
            label = ep.get("label", f"ep{i}")
            out.append({
                "endpoint": ep.get("endpoint", ""),
                "model":    ep.get("model", ""),
                "api_key":  ep.get("api_key", ""),
                "label":    label,
                "enabled":  label not in disabled,
            })
        return out
    # Fallback: einzelner Block aus settings/config
    label = "ep0"
    return [{
        "endpoint": s.get("endpoint", "") or cfg.get("endpoint", ""),
        "model":    s.get("model", "")    or cfg.get("model", ""),
        "api_key":  s.get("api_key", "")  or cfg.get("api_key", ""),
        "label":    label,
        "enabled":  label not in disabled,
    }]


def configured_endpoints() -> list[dict]:
    """Liefert nur die AKTIVEN Endpoints (filtert disabled raus)."""
    return [ep for ep in all_endpoints() if ep.get("enabled", True)]


@app.get("/api/endpoints")
def api_endpoints_list() -> dict:
    return {"endpoints": all_endpoints()}


@app.post("/api/endpoints/toggle")
def api_endpoints_toggle(payload: dict) -> dict:
    label = str(payload.get("label", "")).strip()
    enabled = bool(payload.get("enabled", True))
    if not label:
        raise HTTPException(400, "label fehlt")
    s = load_settings()
    disabled = set(s.get("disabled_endpoints", []))
    if enabled:
        disabled.discard(label)
    else:
        disabled.add(label)
    s["disabled_endpoints"] = sorted(disabled)
    save_settings(s)
    logger.info("endpoint %s -> %s", label, "enabled" if enabled else "DISABLED")
    return {"ok": True, "disabled": sorted(disabled)}


@app.post("/api/llm/check")
def api_llm_check(payload: dict | None = None) -> dict:
    """Prueft EINEN Endpoint (aus payload) oder ALLE konfigurierten."""
    payload = payload or {}
    if payload.get("endpoint"):
        # einzelner Test mit explizit uebergebenen Werten
        try:
            return llm_preflight(
                str(payload["endpoint"]).strip(),
                str(payload.get("model","")).strip(),
                str(payload.get("api_key","")).strip(),
            )
        except Exception as e:
            return {"ok": False, "error": f"interner Fehler: {_exc_msg(e)}",
                    "available_models": [], "sample": None}
    # alle konfigurierten testen
    eps = configured_endpoints()
    results = []
    for ep in eps:
        try:
            r = llm_preflight(ep["endpoint"], ep["model"], ep["api_key"])
        except Exception as e:
            r = {"ok": False, "error": f"interner Fehler: {_exc_msg(e)}",
                 "available_models": [], "sample": None}
        r["endpoint"] = ep["endpoint"]
        r["model"]    = ep["model"]
        r["label"]    = ep["label"]
        results.append(r)
    all_ok = all(r.get("ok") for r in results)
    return {"ok": all_ok, "endpoints": results}

def load_settings() -> dict[str, Any]:
    s = default_settings()
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s

def save_settings(s: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")

def current_root() -> Optional[Path]:
    s = load_settings()
    r = s.get("root", "").strip()
    if not r:
        return None
    p = Path(r).expanduser().resolve()
    if not p.is_dir():
        return None
    init_db(p)  # memoized, cheap nach erstem Call
    return p

def require_root() -> Path:
    r = current_root()
    if r is None:
        raise HTTPException(400, "kein Wurzelverzeichnis gesetzt")
    init_db(r)
    return r

# ---------- tagger subprocess ----------

class Tagger:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.log: deque[str] = deque(maxlen=2000)
        self.log_total: int = 0      # absoluter Lifetime-Counter (steigt monoton)
        self.lock = threading.Lock()
        self.started_at = 0.0
        self.cmd_str = ""

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, settings: dict[str, Any]) -> None:
        with self.lock:
            if self.is_running():
                raise HTTPException(409, "tagger laeuft bereits")
            root = settings.get("root", "").strip()
            if not root or not Path(root).expanduser().is_dir():
                raise HTTPException(400, "ungueltiges Wurzelverzeichnis")
            cmd = [sys.executable, "-u", str(HERE / "tag.py"),
                   str(Path(root).expanduser())]
            # Endpoint-Args nur senden wenn KEINE Multi-Endpoint-Konfig aktiv -
            # sonst wuerde der CLI-Override die Mehrfach-Endpoints aus
            # config.toml/settings.json verdraengen.
            multi = isinstance(load_config().get("llm", {}).get("endpoints"), list) \
                    and len(load_config().get("llm", {}).get("endpoints") or []) > 0
            if not multi:
                if settings.get("endpoint"): cmd += ["--endpoint", settings["endpoint"]]
                if settings.get("model"):    cmd += ["--model",    settings["model"]]
                if settings.get("api_key"):  cmd += ["--api-key",  settings["api_key"]]
            if settings.get("workers"):  cmd += ["--workers",  str(int(settings["workers"]))]
            lim = settings.get("limit")
            if lim not in (None, "", 0): cmd += ["--limit", str(int(lim))]
            if settings.get("retry_errors"): cmd += ["--retry-errors"]
            if settings.get("retag"):    cmd += ["--retag"]
            if settings.get("scan_only"):cmd += ["--scan-only"]
            if settings.get("only"):     cmd += ["--only",  settings["only"]]
            self.log.clear()
            self.log_total = 0
            self._append_line(f"$ {' '.join(cmd)}")
            self.cmd_str = " ".join(cmd)
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
            self.started_at = time.time()
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _append_line(self, line: str) -> None:
        # WICHTIG: log_total ist Lifetime-Counter, deque rotiert bei maxlen.
        self.log.append(line)
        self.log_total += 1

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for line in proc.stdout:
            self._append_line(line.rstrip("\n"))
        rc = proc.wait()
        self._append_line(f"-- tagger exited rc={rc} --")

    def stop(self) -> None:
        with self.lock:
            if not self.is_running():
                return
            assert self.proc
            self._append_line("-- stop requested --")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def tail(self, since: int = 0) -> tuple[list[str], int]:
        """`since` ist ABSOLUTER Lifetime-Counter (was Client zuletzt sah).
        Wir liefern alle Zeilen mit Position >= since, capped durch deque-Inhalt.
        Wenn Client weit zurueckhaengt und Zeilen schon ueberrollt sind,
        bekommt er was noch in der deque ist."""
        all_lines = list(self.log)
        n = len(all_lines)
        first_in_buffer = self.log_total - n   # absolute idx der ersten Zeile in deque
        if since < first_in_buffer:
            since = first_in_buffer            # client hat Zeilen verpasst
        start = since - first_in_buffer
        if start < 0: start = 0
        if start > n: start = n
        return all_lines[start:], self.log_total


tagger = Tagger()

# ---------- API ----------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((HERE / "index.html").read_text(encoding="utf-8"))

@app.get("/api/settings")
def api_get_settings() -> dict:
    s = load_settings()
    s["api_key_set"] = bool(s.get("api_key"))
    s.pop("api_key", None)  # don't expose key to UI
    return s

@app.post("/api/settings")
def api_set_settings(payload: dict) -> dict:
    cur = load_settings()
    allowed = {"root","endpoint","model","api_key","workers","limit",
               "retry_errors","retag","only"}
    for k in allowed:
        if k in payload:
            cur[k] = payload[k]
    # normalize
    if cur.get("limit") in ("", None): cur["limit"] = None
    else:
        try: cur["limit"] = int(cur["limit"])
        except Exception: cur["limit"] = None
    cur["workers"] = int(cur.get("workers") or 4)
    cur["retry_errors"] = bool(cur.get("retry_errors"))
    cur["retag"] = bool(cur.get("retag"))
    cur["only"] = cur.get("only") or ""
    save_settings(cur)
    # init db on new root
    if cur.get("root"):
        p = Path(cur["root"]).expanduser()
        if p.is_dir():
            init_db(p.resolve())
    return {"ok": True}

@app.get("/api/listdir")
def api_listdir(path: str = "") -> dict:
    """Server-side directory browser. path empty -> $HOME."""
    p = Path(path).expanduser() if path else Path.home()
    try:
        p = p.resolve()
    except Exception:
        raise HTTPException(400, "bad path")
    if not p.is_dir():
        raise HTTPException(400, "not a directory")
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "permission denied")
    return {"path": str(p), "parent": str(p.parent), "entries": entries}

@app.get("/api/tags/manual")
def api_manual_tags() -> dict:
    """Liefert die in manual_tags.json definierten Gruppen + Tags."""
    return {"groups": load_manual_tags()}


@app.post("/api/tags/apply")
def api_tags_apply(payload: dict) -> dict:
    """Fuegt einen MANUELLEN Tag an mehrere Files an oder entfernt ihn.
    Schreibt in 'manual_tags' - LLM-Re-Tagging fasst die nicht an.
    payload: {ids: [int], tag: str, action: 'add'|'remove'}"""
    ids = [int(x) for x in (payload.get("ids") or [])]
    tag = str(payload.get("tag") or "").strip().lower()
    action = str(payload.get("action") or "add").lower()
    if not ids:
        raise HTTPException(400, "ids leer")
    if not tag:
        raise HTTPException(400, "tag leer")
    if action not in ("add", "remove"):
        raise HTTPException(400, "action muss 'add' oder 'remove' sein")
    syn = load_synonyms()
    canon = syn.get(tag, tag)

    root = require_root()
    conn = connect(root)
    changed = 0
    try:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, manual_tags FROM files WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        updates = []
        for r in rows:
            current = [t.strip() for t in (r["manual_tags"] or "").split(",") if t.strip()]
            if action == "add":
                if canon not in current:
                    current.append(canon)
                    updates.append((", ".join(current), r["id"]))
            else:  # remove
                if canon in current:
                    new = [t for t in current if t != canon]
                    updates.append((", ".join(new), r["id"]))
        if updates:
            conn.executemany("UPDATE files SET manual_tags=? WHERE id=?", updates)
            conn.commit()
            changed = len(updates)
    finally:
        conn.close()
    logger.info("tags/apply action=%s tag=%s ids=%d changed=%d",
                action, canon, len(ids), changed)
    return {"ok": True, "tag": canon, "action": action,
            "selected": len(ids), "changed": changed}


@app.post("/api/normalize")
def api_normalize() -> dict:
    """Wendet synonyms.json auf alle existierenden Tags in der DB an,
    ohne erneuten LLM-Aufruf. Schnell - reine SQL-Operation."""
    root = current_root()
    if root is None:
        raise HTTPException(400, "kein Wurzelverzeichnis gesetzt")
    syn = load_synonyms()
    if not syn:
        return {"ok": True, "scanned": 0, "changed": 0,
                "note": "synonyms.json leer oder nicht vorhanden"}
    conn = connect(root)
    changed = scanned = 0
    try:
        rows = conn.execute(
            "SELECT id, tags, manual_tags FROM files "
            "WHERE tags<>'' OR manual_tags<>''"
        ).fetchall()
        scanned = len(rows)
        updates: list[tuple[str, str, int]] = []
        for r in rows:
            old_a = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
            old_m = [t.strip() for t in (r["manual_tags"] or "").split(",") if t.strip()]
            new_a = normalize_tags(old_a, syn)
            new_m = normalize_tags(old_m, syn)
            if new_a != old_a or new_m != old_m:
                updates.append((", ".join(new_a), ", ".join(new_m), r["id"]))
        if updates:
            conn.executemany(
                "UPDATE files SET tags=?, manual_tags=? WHERE id=?", updates)
            conn.commit()
            changed = len(updates)
    finally:
        conn.close()
    logger.info("normalize: gescannt=%d geaendert=%d (%d synonym-eintraege)",
                scanned, changed, len(syn))
    return {"ok": True, "scanned": scanned, "changed": changed,
            "synonyms": len(syn)}


@app.post("/api/tagger/start")
def api_tagger_start(payload: dict | None = None) -> dict:
    s = load_settings()
    skip_check = bool((payload or {}).get("skip_check"))
    logger.info("tagger/start angefordert (skip_check=%s, root=%s)",
                skip_check, s.get("root"))
    if not skip_check:
        eps = configured_endpoints()
        failed = []
        for ep in eps:
            chk = llm_preflight(ep["endpoint"], ep["model"], ep["api_key"])
            if not chk.get("ok"):
                failed.append({
                    "label":    ep["label"],
                    "endpoint": ep["endpoint"],
                    "model":    ep["model"],
                    "error":    _safe(chk.get("error") or "kein Detail"),
                })
        if failed:
            msg = "; ".join(f"[{f['label']} {f['endpoint']}@{f['model']}] {f['error']}"
                            for f in failed)
            logger.warning("tagger/start abgebrochen: %s", msg)
            raise HTTPException(
                status_code=422,
                detail={"stage": "preflight", "error": msg, "failed": failed},
            )
    tagger.start(s)
    logger.info("tagger gestartet")
    return {"ok": True}


@app.post("/api/rescan")
def api_rescan() -> dict:
    """Nur Filesystem-Scan, kein LLM. Erkennt neue/geaenderte/umbenannte/
    geloeschte Files und gleicht die DB ab."""
    s = load_settings()
    s = {**s, "scan_only": True}
    logger.info("rescan angefordert root=%s", s.get("root"))
    tagger.start(s)
    return {"ok": True}

@app.post("/api/tagger/stop")
def api_tagger_stop() -> dict:
    tagger.stop()
    return {"ok": True}

@app.get("/api/tagger/log")
def api_tagger_log(since: int = 0) -> dict:
    lines, total = tagger.tail(since)
    return {
        "lines": lines,
        "total": total,
        "running": tagger.is_running(),
        "started_at": tagger.started_at,
        "cmd": tagger.cmd_str,
    }

_STATS_CACHE: dict = {"t": 0.0, "key": "", "data": None}
_STATS_TTL = 5.0   # s

@app.post("/api/db/optimize")
def api_db_optimize() -> dict:
    root = current_root()
    if root is None:
        raise HTTPException(400, "kein Wurzelverzeichnis")
    init_db(root)
    logger.info("db/optimize gestartet")
    t0 = time.time()
    res = optimize_db(root)
    res["elapsed_s"] = round(time.time() - t0, 2)
    logger.info("db/optimize fertig in %.2fs: %s", res["elapsed_s"], res)
    # Stats-Cache invalidieren
    _STATS_CACHE["data"] = None
    return {"ok": True, **res}


@app.get("/api/stats")
def api_stats() -> dict:
    root = current_root()
    if root is None:
        return {"total": 0, "by_type": {}, "no_root": True}
    # 5s-Cache - die UI pollt alle 5s, das war pro Call eine COUNT-Tabelle
    now = time.time()
    key = str(root)
    if (_STATS_CACHE["data"] is not None
            and _STATS_CACHE["key"] == key
            and now - _STATS_CACHE["t"] < _STATS_TTL):
        return _STATS_CACHE["data"]
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT type, status, COUNT(*) c FROM files GROUP BY type, status"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["type"], {})[r["status"]] = r["c"]
        total = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        data = {"total": total, "by_type": out, "root": str(root)}
        _STATS_CACHE.update(t=now, key=key, data=data)
        return data
    finally:
        conn.close()

@app.get("/api/status")
def api_status() -> dict:
    root = current_root()
    if root is None:
        return {"no_root": True, "counts": {}, "processing": [],
                "recent_done": [], "recent_err": [], "rate_per_sec": 0,
                "eta_minutes": None, "now": time.time()}
    conn = connect(root)
    try:
        counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM files GROUP BY status").fetchall()}
        def _row(r):
            d = dict(r)
            for k in ("rel_path", "tags", "error"):
                if k in d and isinstance(d[k], str):
                    d[k] = _safe(d[k])
            return d
        processing = [_row(r) for r in conn.execute(
            "SELECT id, rel_path, type, started_at FROM files "
            "WHERE status='processing' ORDER BY started_at LIMIT 20").fetchall()]
        recent_done = [_row(r) for r in conn.execute(
            "SELECT id, rel_path, type, tags, tagged_at FROM files "
            "WHERE status='done' AND tagged_at>0 "
            "ORDER BY tagged_at DESC LIMIT 10").fetchall()]
        recent_err = [_row(r) for r in conn.execute(
            "SELECT id, rel_path, error FROM files "
            "WHERE status='error' ORDER BY id DESC LIMIT 10").fetchall()]
        now = time.time()
        rate = conn.execute(
            "SELECT COUNT(*) c FROM files WHERE status='done' AND tagged_at > ?",
            (now - 60,)).fetchone()["c"] / 60.0
        pending_n = counts.get("pending", 0) + counts.get("processing", 0)
        eta_min = (pending_n / rate / 60.0) if rate > 0 else None
        return {"counts": counts, "processing": processing,
                "recent_done": recent_done, "recent_err": recent_err,
                "rate_per_sec": rate, "eta_minutes": eta_min, "now": now,
                "root": str(root)}
    finally:
        conn.close()

# ---------- search / files ----------

def fts_query(q: str) -> str:
    q = q.strip()
    if not q: return ""
    tokens = []
    for raw in q.split():
        if raw.startswith('"') and raw.endswith('"') and len(raw) > 2:
            tokens.append(raw); continue
        if raw.upper() in {"AND","OR","NOT"}:
            tokens.append(raw.upper()); continue
        clean = raw.replace('"','')
        if clean: tokens.append(f'"{clean}"*')
    return " ".join(tokens)

@app.get("/api/search")
def api_search(q: str = "", type: Optional[str] = None,
               limit: int = 200, offset: int = 0,
               order: str = "relevance") -> dict:
    root = current_root()
    if root is None:
        return {"results": [], "no_root": True}
    init_db(root)
    limit = max(1, min(limit, 1000))
    conn = connect(root)
    try:
        params: list = []
        where = ["f.status='done'"]
        if type in ("image","video"):
            where.append("f.type=?"); params.append(type)
        order_map = {
            "newest":    "f.mtime DESC",
            "oldest":    "f.mtime ASC",
            "path":      "f.rel_path COLLATE NATSORT ASC",
            "size_desc": "f.size DESC",
            "size_asc":  "f.size ASC",
            "random":    "RANDOM()",
            "relevance": None,   # nur bei Suche; sonst f.id DESC
        }
        order_clause = order_map.get(order, None)

        if q.strip():
            match = fts_query(q)
            base_where = "files_fts MATCH ? AND " + " AND ".join(where)
            count_params = [match] + params
            count_sql = ("SELECT COUNT(*) c FROM files f "
                         "JOIN files_fts ON files_fts.rowid=f.id WHERE " + base_where)
            sql = ("SELECT f.id, f.rel_path, f.type, f.description, "
                   "       f.tags, f.manual_tags, "
                   "       bm25(files_fts) AS rank "
                   "FROM files f JOIN files_fts ON files_fts.rowid=f.id "
                   "WHERE " + base_where +
                   " ORDER BY " + (order_clause or "rank") +
                   " LIMIT ? OFFSET ?")
            params = [match] + params + [limit, offset]
        else:
            base_where = " AND ".join(where)
            count_params = list(params)
            count_sql = "SELECT COUNT(*) c FROM files f WHERE " + base_where
            sql = ("SELECT f.id, f.rel_path, f.type, f.description, "
                   "       f.tags, f.manual_tags, "
                   "       0 AS rank FROM files f WHERE " + base_where +
                   " ORDER BY " + (order_clause or "f.id DESC") +
                   " LIMIT ? OFFSET ?")
            params += [limit, offset]
        total = conn.execute(count_sql, count_params).fetchone()["c"]
        rows = conn.execute(sql, params).fetchall()
        def _split(s): return [_safe(t.strip()) for t in (s or "").split(",") if t.strip()]
        return {"total": total, "offset": offset, "limit": limit,
                "results": [
            {"id": r["id"], "path": _safe(r["rel_path"]), "type": r["type"],
             "description": _safe(r["description"]),
             "tags": _split(r["tags"]),
             "manual_tags": _split(r["manual_tags"])}
            for r in rows]}
    finally:
        conn.close()

@app.get("/api/search/locate")
def api_search_locate(file_id: int, q: str = "",
                       type: Optional[str] = None,
                       order: str = "relevance",
                       max_scan: int = 200000) -> dict:
    """Findet die Position (offset) einer Datei in der aktuellen
    Suchergebnis-Reihenfolge. -1 wenn nicht enthalten oder Suche nicht laeuft."""
    root = current_root()
    if root is None:
        return {"offset": -1}
    init_db(root)
    conn = connect(root)
    try:
        params: list = []
        where = ["f.status='done'"]
        if type in ("image", "video"):
            where.append("f.type=?"); params.append(type)
        order_map = {
            "newest":    "f.mtime DESC",
            "oldest":    "f.mtime ASC",
            "path":      "f.rel_path COLLATE NATSORT ASC",
            "size_desc": "f.size DESC",
            "size_asc":  "f.size ASC",
            "random":    "RANDOM()",
            "relevance": None,
        }
        order_clause = order_map.get(order, None)
        # Bei order=path: in Python sortieren statt via SQLite-Collation
        # (massiv schneller bei vielen Files)
        if order == "path":
            if q.strip():
                match = fts_query(q)
                base_where = "files_fts MATCH ? AND " + " AND ".join(where)
                sql = ("SELECT f.id, f.rel_path "
                       "FROM files f JOIN files_fts ON files_fts.rowid=f.id "
                       "WHERE " + base_where)
                params = [match] + params
            else:
                base_where = " AND ".join(where)
                sql = "SELECT f.id, f.rel_path FROM files f WHERE " + base_where
            rows = conn.execute(sql, params).fetchall()
            rows.sort(key=lambda r: natural_key(r["rel_path"]))
            if len(rows) > int(max_scan):
                rows = rows[:int(max_scan)]
        else:
            if q.strip():
                match = fts_query(q)
                base_where = "files_fts MATCH ? AND " + " AND ".join(where)
                sql = ("SELECT f.id "
                       "FROM files f JOIN files_fts ON files_fts.rowid=f.id "
                       "WHERE " + base_where +
                       " ORDER BY " + (order_clause or "bm25(files_fts)") +
                       " LIMIT ?")
                params = [match] + params + [int(max_scan)]
            else:
                base_where = " AND ".join(where)
                sql = ("SELECT f.id FROM files f WHERE " + base_where +
                       " ORDER BY " + (order_clause or "f.id DESC") + " LIMIT ?")
                params += [int(max_scan)]
            rows = conn.execute(sql, params).fetchall()
        for i, r in enumerate(rows):
            if r["id"] == file_id:
                return {"offset": i, "total_scanned": len(rows)}
        return {"offset": -1, "total_scanned": len(rows)}
    finally:
        conn.close()


@app.get("/api/search/ids")
def api_search_ids(q: str = "", type: Optional[str] = None,
                   order: str = "relevance",
                   max_results: int = 100000) -> dict:
    """Liefert ALLE matchenden Items (leichtgewichtig: id+path+type).
    Fuer Slideshow ueber die ganze Treffermenge."""
    root = current_root()
    if root is None:
        return {"items": [], "no_root": True}
    init_db(root)
    max_results = max(1, min(int(max_results), 200000))
    conn = connect(root)
    try:
        params: list = []
        where = ["f.status='done'"]
        if type in ("image","video"):
            where.append("f.type=?"); params.append(type)
        order_map = {
            "newest":    "f.mtime DESC",
            "oldest":    "f.mtime ASC",
            "path":      "f.rel_path COLLATE NATSORT ASC",
            "size_desc": "f.size DESC",
            "size_asc":  "f.size ASC",
            "random":    "RANDOM()",
            "relevance": None,
        }
        order_clause = order_map.get(order, None)

        # Performance: bei order=path sortieren wir in Python statt via SQLite-
        # Collation. Custom-Collation kostet pro Vergleich einen Python-Callback,
        # bei 50k Files sind das tausende Sekunden langsamer als ein einmaliger
        # Python-Sort der ganzen Liste.
        if order == "path":
            if q.strip():
                match = fts_query(q)
                base_where = "files_fts MATCH ? AND " + " AND ".join(where)
                sql = ("SELECT f.id, f.rel_path, f.type "
                       "FROM files f JOIN files_fts ON files_fts.rowid=f.id "
                       "WHERE " + base_where)
                params = [match] + params
            else:
                base_where = " AND ".join(where)
                sql = "SELECT f.id, f.rel_path, f.type FROM files f WHERE " + base_where
            all_rows = conn.execute(sql, params).fetchall()
            all_rows.sort(key=lambda r: natural_key(r["rel_path"]))
            rows = all_rows[:max_results]
        else:
            if q.strip():
                match = fts_query(q)
                base_where = "files_fts MATCH ? AND " + " AND ".join(where)
                sql = ("SELECT f.id, f.rel_path, f.type "
                       "FROM files f JOIN files_fts ON files_fts.rowid=f.id "
                       "WHERE " + base_where +
                       " ORDER BY " + (order_clause or "bm25(files_fts)") +
                       " LIMIT ?")
                params = [match] + params + [max_results]
            else:
                base_where = " AND ".join(where)
                sql = ("SELECT f.id, f.rel_path, f.type FROM files f WHERE " + base_where +
                       " ORDER BY " + (order_clause or "f.id DESC") +
                       " LIMIT ?")
                params += [max_results]
            rows = conn.execute(sql, params).fetchall()
        return {"items": [{"id": r["id"],
                           "path": _safe(r["rel_path"]),
                           "type": r["type"]} for r in rows]}
    finally:
        conn.close()


def _abs_for(file_id: int) -> tuple[Path, str]:
    root = require_root()
    conn = connect(root)
    try:
        row = conn.execute(
            "SELECT rel_path, type FROM files WHERE id=?", (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row: raise HTTPException(404, "unknown id")
    p = (root / decode_surrogates(row["rel_path"])).resolve()
    if not str(p).startswith(str(root)):
        raise HTTPException(403, "outside root")
    if not p.exists(): raise HTTPException(404, "file missing")
    return p, row["type"]

@app.get("/api/meta/{file_id}")
def api_meta(file_id: int) -> dict:
    """Liefert Beschreibung + Tags fuer eine einzelne Datei."""
    root = current_root()
    if root is None:
        raise HTTPException(400, "kein Wurzelverzeichnis")
    init_db(root)
    conn = connect(root)
    try:
        r = conn.execute(
            "SELECT id, rel_path, type, description, tags, manual_tags "
            "FROM files WHERE id=?", (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if not r:
        raise HTTPException(404, "unknown id")
    def _split(s): return [_safe(t.strip()) for t in (s or "").split(",") if t.strip()]
    return {
        "id": r["id"],
        "path": _safe(r["rel_path"]),
        "type": r["type"],
        "description": _safe(r["description"]),
        "tags": _split(r["tags"]),
        "manual_tags": _split(r["manual_tags"]),
    }


@app.get("/thumb/{file_id}")
def serve_thumb(file_id: int):
    root = require_root()
    tp = thumb_path(root, file_id)
    if tp.exists():
        return FileResponse(tp, media_type="image/jpeg")
    raise HTTPException(404, "no thumb")

@app.get("/file/{file_id}")
def serve_file(file_id: int):
    p, _ = _abs_for(file_id)
    return FileResponse(p)

@app.post("/api/open")
def api_open(payload: dict) -> dict:
    fid = int(payload.get("id", 0))
    p, ftype = _abs_for(fid)
    # Pro-Typ Viewer aus config.toml; leer -> xdg-open
    viewers = load_config().get("viewers", {}) or {}
    cmd_str = (viewers.get(ftype) or "").strip()
    if cmd_str:
        import shlex
        cmd = shlex.split(cmd_str) + [str(p)]
    else:
        cmd = ["xdg-open", str(p)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise HTTPException(500, f"viewer nicht gefunden: {cmd[0]} ({e})")
    except Exception as e:
        raise HTTPException(500, f"open failed: {e}")
    return {"ok": True}

@app.get("/api/duplicates")
def api_duplicates(mode: str = "exact", threshold: int = 6,
                   limit_groups: int = 200,
                   type: Optional[str] = None) -> dict:
    """Findet Doubletten.
    mode='exact'   - identische Files (gleicher content_hash)
    mode='near'    - aehnliche Bilder/Videos (Hamming-Distance phash <= threshold)
    type='image'|'video'|None  - filtert auf Medientyp
    Liefert Liste von Gruppen, jede mit den Mitgliedern (id, path, type, size)."""
    root = current_root()
    if root is None:
        raise HTTPException(400, "kein Wurzelverzeichnis")
    init_db(root)
    threshold = max(0, min(int(threshold), 32))
    type_filter = type if type in ("image", "video") else None

    conn = connect(root)
    try:
        if mode == "exact":
            type_clause = " AND type=?" if type_filter else ""
            params: list = [limit_groups]
            if type_filter:
                params = [type_filter, limit_groups]
            rows = conn.execute(f"""
                SELECT content_hash, COUNT(*) c FROM files
                WHERE content_hash IS NOT NULL AND content_hash <> ''
                {type_clause}
                GROUP BY content_hash HAVING c > 1
                ORDER BY c DESC LIMIT ?
            """, params).fetchall()
            groups = []
            for hr in rows:
                ch = hr["content_hash"]
                m_params: list = [ch]
                m_clause = ""
                if type_filter:
                    m_clause = " AND type=?"
                    m_params.append(type_filter)
                members = conn.execute(
                    "SELECT id, rel_path, type, size FROM files "
                    f"WHERE content_hash=?{m_clause} ORDER BY mtime ASC",
                    m_params).fetchall()
                groups.append({
                    "key":     ch[:12],
                    "count":   hr["c"],
                    "members": [{"id": m["id"], "path": _safe(m["rel_path"]),
                                 "type": m["type"], "size": m["size"]}
                                for m in members],
                })
            return {"mode": "exact", "groups": groups}

        # mode='near': perceptual hash hamming distance.
        # Bilder werden direkt gehasht, Videos ueber den mittleren Frame.
        # Wir gruppieren ueber alle Files mit phash gemeinsam, da ein Video
        # und ein extrahiertes Standbild oft denselben pHash haben (legitim).
        sql = ("SELECT id, rel_path, type, size, phash_int FROM files "
               "WHERE phash_int IS NOT NULL")
        params = []
        if type_filter:
            sql += " AND type=?"
            params.append(type_filter)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return {"mode": "near", "groups": [],
                    "note": "keine perceptual-hashes - bitte zuerst dedupe.py laufen lassen"}
        # numpy-Vektorisierung: 64-bit popcount via XOR
        try:
            import numpy as np
        except ImportError:
            return {"mode": "near", "groups": [], "note": "numpy fehlt"}
        ids   = np.array([r["id"] for r in rows], dtype=np.int64)
        paths = [_safe(r["rel_path"]) for r in rows]
        sizes = [r["size"] for r in rows]
        types = [r["type"] for r in rows]
        # phash_int kann negativ (signed) sein - in unsigned konvertieren
        ph = np.array([r["phash_int"] & 0xFFFFFFFFFFFFFFFF for r in rows],
                      dtype=np.uint64)
        # Cluster aufbauen: jedes file zur Gruppe seines ersten Match-Vorgaengers
        # Performance: O(N^2) mit numpy-XOR/popcount, OK bis ~100k
        n = len(ph)
        cluster = np.full(n, -1, dtype=np.int32)
        cur_id = 0
        for i in range(n):
            if cluster[i] >= 0: continue
            cluster[i] = cur_id
            # Hamming distances of i vs i+1..n
            xor = ph[i+1:] ^ ph[i]
            # popcount per element via bytes
            xb  = xor.view(np.uint8).reshape(-1, 8)
            popc = np.unpackbits(xb, axis=1).sum(axis=1)
            mask = popc <= threshold
            for j_offset, m in enumerate(mask):
                if not m: continue
                j = i + 1 + j_offset
                if cluster[j] < 0:
                    cluster[j] = cur_id
            cur_id += 1
        # Gruppen sammeln
        groups: dict[int, list[int]] = {}
        for idx, gid in enumerate(cluster):
            groups.setdefault(int(gid), []).append(idx)
        out = []
        for gid, members in groups.items():
            if len(members) < 2: continue
            out.append({
                "key":     f"phash-{gid}",
                "count":   len(members),
                "members": [{"id": int(ids[m]), "path": paths[m],
                             "type": types[m], "size": sizes[m]}
                            for m in members],
            })
        out.sort(key=lambda g: -g["count"])
        return {"mode": "near", "groups": out[:limit_groups]}
    finally:
        conn.close()


@app.post("/api/duplicates/delete")
def api_duplicates_delete(payload: dict) -> dict:
    """Loescht angegebene Files (von Disk + DB) - DESTRUKTIV.
    payload: {ids: [int]}"""
    ids = [int(x) for x in (payload.get("ids") or [])]
    if not ids:
        raise HTTPException(400, "keine ids")
    root = require_root()
    deleted = errors = 0
    conn = connect(root)
    try:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, rel_path FROM files WHERE id IN ({ph})", ids
        ).fetchall()
        for r in rows:
            p = (root / decode_surrogates(r["rel_path"])).resolve()
            if not str(p).startswith(str(root)):
                continue   # safety
            try:
                if p.exists():
                    p.unlink()
                # Thumb auch raus
                try: thumb_path(root, r["id"]).unlink(missing_ok=True)
                except Exception: pass
                conn.execute("DELETE FROM files WHERE id=?", (r["id"],))
                deleted += 1
            except Exception as e:
                logger.error("delete %s failed: %s", p, e)
                errors += 1
        conn.commit()
    finally:
        conn.close()
    logger.info("duplicates/delete: %d deleted, %d errors", deleted, errors)
    return {"ok": True, "deleted": deleted, "errors": errors}


@app.post("/api/lora-export")
def api_lora_export(payload: dict) -> dict:
    """Exportiert ausgewaehlte Bilder + Tag-Sidecars fuer LoRA-Training.
    payload: {
      ids: [int],
      dest_name: str,            # Ordnername unter <scripts>/lora_exports/
      trigger_word: str,         # optional, wird vor die Tags gesetzt
      rename_seq: bool,          # 0001.jpg, 0002.jpg statt Originalname
      include_description: bool, # Beschreibung als 1. Zeile, dann Tags
      include_auto_tags: bool,   # auto-Tags mit aufnehmen (default true)
      max_tags: int,             # Limit pro Sidecar (0 = unlimitiert)
    }
    """
    import shutil, time as _t, re as _re
    ids = [int(x) for x in (payload.get("ids") or [])]
    if not ids:
        raise HTTPException(400, "ids leer")
    dest_name = str(payload.get("dest_name") or "").strip()
    if not dest_name:
        dest_name = "lora-export-" + _t.strftime("%Y%m%d-%H%M%S")
    dest_name = _re.sub(r"[^A-Za-z0-9._-]", "_", dest_name)[:80] or "lora-export"
    trigger_word        = str(payload.get("trigger_word") or "").strip().lower()
    rename_seq          = bool(payload.get("rename_seq", True))
    include_description = bool(payload.get("include_description", False))
    include_auto_tags   = bool(payload.get("include_auto_tags", True))
    max_tags            = int(payload.get("max_tags") or 0)

    root = require_root()
    dest_root = HERE / "lora_exports" / dest_name
    dest_root.mkdir(parents=True, exist_ok=True)

    conn = connect(root)
    try:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, rel_path, type, description, tags, manual_tags "
            f"FROM files WHERE id IN ({ph}) ORDER BY id", ids
        ).fetchall()
    finally:
        conn.close()

    exported = skipped = 0
    skipped_paths: list[str] = []
    seq = 0
    for r in rows:
        if r["type"] != "image":
            skipped += 1
            skipped_paths.append(_safe(r["rel_path"]))
            continue
        seq += 1
        from common import decode_surrogates as _dec
        src = (root / _dec(r["rel_path"])).resolve()
        if not src.exists():
            skipped += 1
            continue
        ext = src.suffix.lower() or ".jpg"
        if rename_seq:
            new_stem = f"{seq:04d}"
        else:
            # bei Originalnamen: invalid-utf8-zeichen rauswerfen
            new_stem = _re.sub(r"[^A-Za-z0-9._-]", "_",
                               _safe(src.stem))[:120] or f"{seq:04d}"
        dst_image = dest_root / f"{new_stem}{ext}"
        dst_text  = dest_root / f"{new_stem}.txt"
        try:
            shutil.copy2(src, dst_image)
        except Exception as e:
            logger.error("lora-export copy failed: %s -> %s: %s", src, dst_image, e)
            skipped += 1
            continue
        # Tags zusammenstellen: trigger_word, manuelle (priorisiert), dann auto
        manual = [t.strip() for t in (r["manual_tags"] or "").split(",") if t.strip()]
        auto   = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
        tag_list: list[str] = []
        if trigger_word:
            tag_list.append(trigger_word)
        tag_list.extend(manual)
        if include_auto_tags:
            tag_list.extend(auto)
        # dedupliziere unter Erhalt der Reihenfolge
        seen: set[str] = set()
        uniq = [t for t in tag_list if t and not (t in seen or seen.add(t))]
        if max_tags > 0:
            uniq = uniq[:max_tags]
        body = ", ".join(uniq)
        if include_description and r["description"]:
            body = _safe(r["description"]).replace("\n", " ") + "\n\n" + body
        dst_text.write_text(body + "\n", encoding="utf-8")
        exported += 1

    logger.info("lora-export: %d files -> %s (skipped=%d)",
                exported, dest_root, skipped)
    return {"ok": True, "exported": exported, "skipped": skipped,
            "dest": str(dest_root),
            "skipped_paths": skipped_paths[:20]}


@app.post("/api/play")
def api_play(payload: dict) -> dict:
    ids = payload.get("ids") or []
    if not ids: raise HTTPException(400, "no ids")
    paths: list[str] = []
    for fid in ids:
        try:
            p, _ = _abs_for(int(fid))
            paths.append(str(p))
        except HTTPException:
            continue
    if not paths: raise HTTPException(404, "none of the ids resolved")
    # Eines = endlos loopen bis Esc; mehrere = playlist mit shuffle+loop
    # --fs = fullscreen sofort beim Start
    # --input-conf = unsere custom Hotkeys (z.B. b/n fuer Rotation)
    mpv_extra: list[str] = ["--fs"]
    custom_input = (HERE / "mpv" / "input.conf").resolve()
    if custom_input.is_file():
        mpv_extra.append(f"--input-conf={custom_input}")
        logger.info("mpv input-conf: %s", custom_input)
    else:
        logger.warning("mpv input-conf NICHT gefunden: %s", custom_input)
    if len(paths) == 1:
        cmd = ["mpv", *mpv_extra, "--loop-file=inf", paths[0]]
    else:
        random.shuffle(paths)
        cmd = ["mpv", *mpv_extra, "--shuffle", "--loop-playlist=inf", *paths]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HTTPException(500, "mpv not installed")
    return {"ok": True, "count": len(paths)}

# ---------- main ----------

def main() -> None:
    cfg = load_config().get("serve", {})
    ap = argparse.ArgumentParser(description="mediasearch web UI")
    ap.add_argument("root", nargs="?", default=None,
                    help="(optional) Wurzelverzeichnis. Sonst im UI auswaehlen.")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    host = args.host or cfg.get("host", "127.0.0.1")
    port = args.port if args.port is not None else int(cfg.get("port", 8765))
    if args.root:
        s = load_settings(); s["root"] = str(Path(args.root).expanduser().resolve())
        save_settings(s)
    # init_db einmalig pro Root, falls schon konfiguriert
    r = current_root()
    if r:
        logger.info("DB-Slot: %s", db_path(r))
        old_legacy = r / ".mediasearch"
        if old_legacy.exists():
            logger.info("Hinweis: legacy %s kann geloescht werden "
                        "(neue DB liegt unter %s)",
                        old_legacy, DATA_DIR)
    print(f"open http://{host}:{port}")
    import uvicorn
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main()
