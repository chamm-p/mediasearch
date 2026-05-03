"""Crawler + LLM-Tagger.

Inkrementell: Files mit gleichem rel_path + size + mtime werden uebersprungen,
sofern bereits status='done'. Geaenderte Files werden automatisch neu getaggt.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

# stdout robust gegen invalid-UTF-8 in Filenames (lone surrogates)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _safe(s: str) -> str:
    if not s:
        return s
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", errors="replace").decode("utf-8")

from common import (
    connect,
    decode_surrogates,
    encode_surrogates,
    find_context,
    image_to_jpeg_bytes,
    init_db,
    iter_media,
    load_config,
    load_disabled_endpoints,
    load_synonyms,
    make_thumb,
    media_type,
    normalize_tags,
    thumb_path,
    video_frames_for_tagging,
)

PROMPT = (
    "Du bist ein praeziser Medien-Tagger. Du erhaelst entweder ein Bild oder "
    "zwei Frames aus einem Video.\n\n"
    "AUSGABE-REGELN (strikt einhalten):\n"
    "1) Gib GENAU EIN JSON-Objekt zurueck. NICHTS davor, NICHTS danach.\n"
    "2) Kein Markdown, keine Codeblock-Fences (kein ```json), keine "
    "Erklaerungen, keine Anrede, keine Schlussformel.\n"
    "3) Sprache: Deutsch.\n\n"
    "JSON-SCHEMA:\n"
    '{"description": "<MAXIMAL ZWEI knappe Saetze, zusammen unter 200 '
    'Zeichen>", "tags": ["<kleinbuchstabe>", "<schlagwort>", "..."]}\n\n'
    "TAG-REGELN:\n"
    "- 8 bis 25 Tags, alles lowercase, kurze Woerter oder Nominalphrasen.\n"
    "- Genau ein Oberbegriff pro Konzept, KEINE Synonyme. Beispiele: 'boot' "
    "(nicht zusaetzlich 'schiff', 'dampfer', 'yacht'); 'auto' (nicht "
    "'wagen', 'pkw', 'fahrzeug'); 'kind' (nicht 'junge', 'maedchen', "
    "'kleinkind').\n"
    "- Bevorzuge das gebraeuchlichste deutsche Wort.\n"
    "- Bei Personen keine Identifikation, nur generisch ('kind', 'frau', "
    "'gruppe').\n"
    "- Decke ab: Motive, Objekte, Handlungen, Setting, dominante Farben, "
    "Lichtstimmung, Stil, Atmosphaere, ggf. sichtbarer Text.\n"
    "- Keine Duplikate.\n\n"
    "BESCHREIBUNG-REGELN:\n"
    "- Strikt 1-2 Saetze, sachlich, ohne Wertung, ohne Floskeln "
    "('Auf dem Bild sehen wir...' weglassen).\n"
    "- Beschreibe ausschliesslich das tatsaechlich sichtbare Bild. "
    "Falls eine system message Hintergrundkontext enthaelt: NICHT abschreiben, "
    "NICHT wiederholen - nur als Interpretationshilfe nutzen.\n"
)


def b64_data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


_DECODER = json.JSONDecoder()

# Reasoning-/Thinking-Muster, die wir aus Roh-Antworten entfernen
_THINK_TAG_RE = re.compile(r"<think(?:ing)?[^>]*>.*?</think(?:ing)?>",
                           re.DOTALL | re.IGNORECASE)
_REASONING_PHRASES = (
    "thinking process", "let me analyze", "let me think", "step 1",
    "step-by-step", "schritt 1", "analyse:", "analysis:", "approach:",
    "first, i", "1. analyse", "1. **",
)


def strip_reasoning(text: str) -> str:
    """Entfernt <think>-Tags und typische Reasoning-Praeambeln, sowohl
    multi-line als auch inline."""
    if not text:
        return text
    s = _THINK_TAG_RE.sub("", text)
    # Inline-Praeambeln: 'Here is a thinking process. 1. **X** 2. **Y** Echter Text...'
    # -> entferne den letzten "**...**"-Block-Abschnitt, behalte was danach kommt
    s = re.sub(
        r"^\s*(?:here is|let me|let's|hier ist)[^.\n]*(?:thinking|reasoning|"
        r"analysis|denkprozess|step|schritt|process)[^.\n]*\.\s*",
        "", s, flags=re.IGNORECASE,
    )
    # Wiederholt nummerierte Bold-Header am Anfang abschneiden
    while True:
        new = re.sub(r"^\s*\d+\.\s*\*\*[^*\n]+\*\*\s*", "", s)
        new = re.sub(r"^\s*\*\*\d+\.\s*[^*\n]+\*\*\s*", "", new)
        new = re.sub(r"^\s*(?:step|schritt)\s*\d+[:.][^\n.]*\.\s*", "",
                     new, flags=re.IGNORECASE)
        if new == s:
            break
        s = new
    # Multi-line Reasoning (Skip Zeilen mit Bullets/Headern)
    low = s.lstrip().lower()[:80]
    if any(p in low for p in _REASONING_PHRASES):
        kept: list[str] = []
        for line in s.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^(?:#{1,3}\s|[*\-]\s|\d+[.)]\s|step\s*\d+|schritt\s*\d+|"
                        r"\*\*\d+|<think|analyse:|analysis:|let me|first,|hier ist)",
                        stripped, re.IGNORECASE):
                continue
            kept.append(stripped)
        s = " ".join(kept) if kept else s
    return s.strip()


def parse_response(text: str) -> tuple[str, list[str]]:
    """Robust gegen praeambeln, code fences, nachgereihten kommentar.
    Findet das erste vollstaendige JSON-Objekt im Text."""
    if not text:
        return "", []
    s = text.strip()
    # leading code fence weg
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    # ersten '{' suchen, dann mit raw_decode greedy bis zum erstem gueltigen Object
    start = s.find("{")
    if start < 0:
        raise ValueError("kein '{' in LLM-Antwort gefunden")
    last_err: Exception | None = None
    data = None
    while start >= 0:
        try:
            obj, _end = _DECODER.raw_decode(s[start:])
            # nur akzeptieren wenn Object mit erwarteten keys
            if isinstance(obj, dict) and ("tags" in obj or "description" in obj):
                data = obj
                break
        except json.JSONDecodeError as e:
            last_err = e
        start = s.find("{", start + 1)
    if data is None:
        raise last_err or ValueError("kein gueltiges Tag-JSON gefunden")
    desc = str(data.get("description", "")).strip()
    # Reasoning-Praefixe entfernen, falls Modell sowas in description packt
    desc = strip_reasoning(desc) or desc
    # Beschreibung haerten: cap auf 300 Zeichen falls Modell trotzdem ausschweift
    if len(desc) > 300:
        desc = desc[:297].rstrip() + "..."
    raw_tags = data.get("tags", [])
    tags = []
    seen = set()
    for t in raw_tags:
        t = str(t).strip().lower()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    # Tag-Limit: cap auf 30
    return desc, tags[:30]


def call_llm(client: OpenAI, model: str, jpegs: list[bytes],
             synonyms: dict[str, str] | None = None,
             context_hint: str = ""
             ) -> tuple[str, list[str], str]:
    """Returns (description, tags, raw_text_or_diag)."""
    messages: list[dict] = []
    if context_hint:
        messages.append({
            "role": "system",
            "content": (
                "Hintergrundwissen zum Verzeichnis dieses Bildes (NUR zur "
                "Disambiguierung beim Tagging - z.B. um zwischen 'hochzeitstorte' "
                "und 'geburtstagstorte' zu unterscheiden):\n\n"
                f"{context_hint}\n\n"
                "REGEL: Beschreibe in deiner Antwort AUSSCHLIESSLICH das "
                "gezeigte Bild. Schreibe NICHT vom Hintergrundwissen ab, "
                "wiederhole es nicht und erfinde keine Inhalte daraus. "
                "Wenn das Bild zum Hintergrund passt - super, dann prazisiere "
                "die Tags entsprechend. Wenn nicht - ignoriere den Hintergrund."
            ),
        })
    # Qwen3-spezifische Inline-Direktive: '/no_think' deaktiviert das
    # Thinking-Token-Praefix bei Qwen3-Familien-Modellen direkt im Prompt.
    user_text = "/no_think\n\n" + PROMPT
    content = [{"type": "text", "text": user_text}]
    for jpg in jpegs:
        content.append({"type": "image_url", "image_url": {"url": b64_data_url(jpg)}})
    messages.append({"role": "user", "content": content})
    # Assistant-Prefill: Wenn ein Modell auf Reasoning hartcodiert ist, hilft
    # es oft, die Antwort vorzugeben - das Modell muss dann nur weiterschreiben
    # und ueberspringt das Reasoning. Server, die das nicht unterstuetzen,
    # ignorieren den Eintrag oder behandeln ihn als zusaetzlichen Turn (unkritisch).
    messages.append({"role": "assistant", "content": '{"description": "'})
    # Qwen3-Familie hat Thinking-Mode -> Output landet in reasoning_content
    # statt content. Mit extra_body "enable_thinking": false bitten wir vLLM,
    # direkt zu antworten. Wenn der Server's nicht versteht, ignoriert er's.
    # Mehrere Reasoning-Disabler stacken - der Server nimmt das was er versteht:
    # 1) chat_template_kwargs.enable_thinking - vLLM/Qwen3 chat template
    # 2) reasoning_effort - OpenAI-Style API
    # 3) /no_think inline im Prompt - bereits in user_text gesetzt (s.o.)
    extra = {
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "none",
        # vLLM-spezifisch: weiter im letzten assistant-message-Inhalt
        "continue_final_message": True,
        "add_generation_prompt": False,
    }
    # Per-Request Timeout (Sekunden). Verhindert dass haengende Endpoints
    # permanent Worker blockieren. Override via env: MEDIASEARCH_TAG_TIMEOUT
    import os as _os
    req_timeout = float(_os.getenv("MEDIASEARCH_TAG_TIMEOUT", "180"))
    # max_tokens: Reasoning-Modelle (Qwen3 etc.) brauchen reichlich Budget,
    # weil sie viele Tokens fuers Thinking verbrauchen bevor sie die finale
    # Antwort schreiben. Override via env: MEDIASEARCH_TAG_MAXTOKENS
    max_tok = int(_os.getenv("MEDIASEARCH_TAG_MAXTOKENS", "4000"))
    try:
        resp = client.with_options(timeout=req_timeout).chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=max_tok,
            extra_body=extra,
        )
    except Exception:
        # Server akzeptiert extra_body nicht -> nochmal ohne
        resp = client.with_options(timeout=req_timeout).chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=max_tok,
        )
    msg = resp.choices[0].message
    text = msg.content or ""
    # Wenn Assistant-Prefill akzeptiert wurde, fehlt das fuehrende '{"description": "'
    # in der Antwort. Vorne wieder ergaenzen falls noetig.
    if text and not text.lstrip().startswith("{"):
        text = '{"description": "' + text
    # Fallback: manche Server liefern Antwort in reasoning_content/reasoning
    if not text:
        for attr in ("reasoning_content", "reasoning"):
            v = getattr(msg, attr, None)
            if v:
                text = str(v)
                break
    if not text:
        # Diagnose-Dump zurueckgeben statt nur "leer"
        try:
            dump = resp.choices[0].model_dump_json()[:600]
        except Exception:
            dump = repr(resp.choices[0])[:600]
        finish = getattr(resp.choices[0], "finish_reason", "?")
        return "", [], f"<empty content; finish_reason={finish}; dump={dump}>"
    try:
        desc, tags = parse_response(text)
    except Exception:
        desc, tags = "", []
    tags = normalize_tags(tags, synonyms)
    return desc, tags, text


class EndpointPool:
    """Vergibt Endpoints thread-safe nach 'least-busy': der Endpoint mit
    den wenigsten gerade laufenden Calls bekommt den naechsten. So ueber-
    nehmen schnelle Endpoints automatisch mehr Last, ohne dass langsame
    den Gesamtdurchsatz drosseln. Ties brechen bei Schnellsten zuerst."""
    def __init__(self, specs: list[dict]):
        import threading
        self.specs       = specs
        self.in_flight   = [0] * len(specs)
        self.completed   = [0] * len(specs)
        self.total_time  = [0.0] * len(specs)
        self.lock        = threading.Lock()

    def pick(self) -> tuple[dict, int]:
        if not self.specs:
            raise RuntimeError("kein LLM-Endpoint konfiguriert")
        with self.lock:
            # waehle den mit minimaler in-flight-Zahl, bei Gleichstand
            # den mit kuerzerer durchschnittlicher Bearbeitungszeit
            def key(j: int) -> tuple[int, float]:
                inflight = self.in_flight[j]
                avg = (self.total_time[j] / self.completed[j]
                       if self.completed[j] else 0.0)
                return (inflight, avg)
            i = min(range(len(self.specs)), key=key)
            self.in_flight[i] += 1
            return self.specs[i], i

    def release(self, idx: int, elapsed: float) -> None:
        with self.lock:
            self.in_flight[idx] = max(0, self.in_flight[idx] - 1)
            self.completed[idx] += 1
            self.total_time[idx] += elapsed

    def stats(self) -> list[dict]:
        with self.lock:
            return [{
                "label":    s["label"],
                "in_flight": self.in_flight[i],
                "completed": self.completed[i],
                "avg_s":    (self.total_time[i] / self.completed[i]
                             if self.completed[i] else 0.0),
            } for i, s in enumerate(self.specs)]


def discover(root: Path) -> tuple[int, int, int, int, int]:
    """Walk filesystem, upsert files. Returns (new, changed, moved, removed, total).

    - Renames werden erkannt (gleiche size+mtime in einem nicht mehr sichtbaren Pfad
      und einem neuen Pfad) -> alter DB-Eintrag wird auf neuen Pfad umgezogen,
      Tags bleiben erhalten.
    - Verschwundene Files werden aus der DB geloescht (samt Thumbnail).
    """
    conn = connect(root)
    total = 0
    last_print = time.time()
    now = time.time()

    try:
        print("Scan: lade bekannte Files aus DB...", flush=True)
        known: dict[str, tuple] = {
            r["rel_path"]: (r["id"], r["size"], r["mtime"])
            for r in conn.execute(
                "SELECT id, rel_path, size, mtime FROM files"
            ).fetchall()
        }
        print(f"Scan: {len(known)} bekannte Files; walke {root}...", flush=True)

        seen_rel: set[str] = set()
        new_files: list[tuple[str, str, int, float]] = []   # (rel, kind, size, mtime)
        changed_rows: list[tuple[int, int, float]] = []     # (id, size, mtime)
        seen_ids: list[int] = []

        for p in iter_media(root):
            total += 1
            try:
                st = p.stat()
            except OSError:
                continue
            rel = encode_surrogates(str(p.relative_to(root)))
            seen_rel.add(rel)
            row = known.get(rel)
            if row is None:
                new_files.append((rel, media_type(p), st.st_size, st.st_mtime))
            else:
                fid, old_size, old_mtime = row
                if old_size != st.st_size or abs(old_mtime - st.st_mtime) > 1:
                    changed_rows.append((fid, st.st_size, st.st_mtime))
                else:
                    seen_ids.append(fid)

            now_t = time.time()
            if now_t - last_print >= 2.0:
                print(f"Scan: {total} Files gesehen "
                      f"(neu={len(new_files)} geaendert={len(changed_rows)})",
                      flush=True)
                last_print = now_t

        # Orphans = bekannte Eintraege deren Pfad nicht mehr vorkommt
        orphans = [
            (fid, rel, size, mtime)
            for rel, (fid, size, mtime) in known.items()
            if rel not in seen_rel
        ]

        # Renames erkennen: orphan + new_file mit gleicher (size, ~mtime)
        new_by_sig: dict[tuple[int, int], list[int]] = {}
        for idx, (rel, _kind, size, mtime) in enumerate(new_files):
            new_by_sig.setdefault((size, int(mtime)), []).append(idx)

        moves: list[tuple[str, float, int, int]] = []  # (new_rel, now, fid, idx-zu-entfernen)
        consumed_new: set[int] = set()
        for fid, old_rel, size, mtime in orphans[:]:
            key = (size, int(mtime))
            cands = new_by_sig.get(key)
            if not cands:
                continue
            while cands:
                idx = cands.pop(0)
                if idx in consumed_new:
                    continue
                new_rel = new_files[idx][0]
                moves.append((new_rel, now, fid, idx))
                consumed_new.add(idx)
                orphans.remove((fid, old_rel, size, mtime))
                break

        # Inserts ohne die als rename erkannten
        inserts = [
            (rel, kind, size, mtime, now)
            for i, (rel, kind, size, mtime) in enumerate(new_files)
            if i not in consumed_new
        ]

        print(f"Scan: schreibe DB - inserts={len(inserts)} changed="
              f"{len(changed_rows)} moves={len(moves)} removed={len(orphans)} "
              f"unchanged={len(seen_ids)}", flush=True)

        if inserts:
            conn.executemany(
                "INSERT INTO files(rel_path, type, size, mtime, seen_at, status) "
                "VALUES(?, ?, ?, ?, ?, 'pending')",
                inserts,
            )
        if changed_rows:
            conn.executemany(
                "UPDATE files SET size=?, mtime=?, status='pending', "
                "error='', seen_at=? WHERE id=?",
                [(s, m, now, fid) for fid, s, m in changed_rows],
            )
        if moves:
            conn.executemany(
                "UPDATE files SET rel_path=?, seen_at=? WHERE id=?",
                [(new_rel, now_, fid) for new_rel, now_, fid, _ in moves],
            )
        if orphans:
            # Thumbnails vor DB-DELETE wegraeumen
            for fid, _rel, _s, _m in orphans:
                try:
                    thumb_path(root, fid).unlink(missing_ok=True)
                except Exception:
                    pass
            conn.executemany(
                "DELETE FROM files WHERE id=?",
                [(fid,) for fid, _r, _s, _m in orphans],
            )
        if seen_ids and len(seen_ids) <= 5000:
            conn.executemany(
                "UPDATE files SET seen_at=? WHERE id=?",
                [(now, fid) for fid in seen_ids],
            )
        conn.commit()
        return len(inserts), len(changed_rows), len(moves), len(orphans), total
    finally:
        conn.close()


def mark_processing(root: Path, file_id: int) -> None:
    conn = connect(root)
    try:
        conn.execute(
            "UPDATE files SET status='processing', started_at=? WHERE id=?",
            (time.time(), file_id),
        )
        conn.commit()
    finally:
        conn.close()


def tag_one(root: Path, file_id: int, rel_path: str, kind: str,
            pool: "EndpointPool",
            synonyms: dict[str, str] | None = None
            ) -> tuple[int, str, str, list[str], str | None, str]:
    """Returns (id, rel_path, description, tags, error_or_None, ep_label)."""
    mark_processing(root, file_id)
    abs_path = root / decode_surrogates(rel_path)
    spec, ep_idx = pool.pick()
    ep_label = spec["label"]
    t_start = time.time()
    if not abs_path.exists():
        pool.release(ep_idx, time.time() - t_start)
        _store_error(root, file_id, "missing")
        return file_id, rel_path, "", [], "missing", ep_label
    try:
        if kind == "image":
            jpg = image_to_jpeg_bytes(abs_path)
            if not jpg:
                pool.release(ep_idx, time.time() - t_start)
                _store_error(root, file_id, "decode_failed")
                return file_id, rel_path, "", [], "decode_failed", ep_label
            jpegs = [jpg]
        else:
            jpegs = video_frames_for_tagging(abs_path)
            if not jpegs:
                pool.release(ep_idx, time.time() - t_start)
                _store_error(root, file_id, "frame_extract_failed")
                return file_id, rel_path, "", [], "frame_extract_failed", ep_label
        ctx_hint = find_context(root, abs_path)
        desc, tags, raw = call_llm(spec["client"], spec["model"], jpegs,
                                    synonyms=synonyms, context_hint=ctx_hint)
        pool.release(ep_idx, time.time() - t_start)

        # Wenn LLM-Antwort komplett leer ist (kein Content, kein Reasoning):
        # echter Fehler.
        is_empty_response = (not raw) or raw.startswith("<empty content")
        if is_empty_response and not desc and not tags:
            err = f"empty_response_from_{ep_label}: {raw or '<leer>'}"[:500]
            print(f"    [{ep_label}] RAW: {raw or '<leer>'}", flush=True)
            _store_error(root, file_id, err)
            return file_id, rel_path, desc, tags, err, ep_label

        # Tags leer:
        # - desc kommt nur aus erfolgreichem JSON-Parsing (kein Roh-Text-Fallback,
        #   sonst landet Reasoning-Geschwaetz in der DB)
        # - falls auch desc leer -> Fehler mit Roh-Antwort als Diagnose
        if not tags:
            if not desc:
                err = (f"unparseable_from_{ep_label}: "
                       + raw.replace("\n", " \\n ")[:300])
                print(f"    [{ep_label}] ERR (unparseable): "
                      + raw.replace("\n", " \\n ")[:200], flush=True)
                _store_error(root, file_id, err)
                return file_id, rel_path, "", [], err, ep_label
            preview = desc.replace("\n", " \\n ")[:200]
            print(f"    [{ep_label}] WARN no tags, saved desc-only: {preview}",
                  flush=True)
        # thumbnail (best-effort)
        try:
            make_thumb(abs_path, kind, thumb_path(root, file_id))
        except Exception:
            pass
        conn = connect(root)
        try:
            conn.execute(
                "UPDATE files SET description=?, tags=?, status='done', "
                "error='', tagged_at=? WHERE id=?",
                (desc, ", ".join(tags), time.time(), file_id),
            )
            conn.commit()
        finally:
            conn.close()
        return file_id, rel_path, desc, tags, None, ep_label
    except Exception as e:
        pool.release(ep_idx, time.time() - t_start)
        msg = f"[{ep_label}] {type(e).__name__}: {e}"[:500]
        _store_error(root, file_id, msg)
        return file_id, rel_path, "", [], msg, ep_label


def _store_error(root: Path, file_id: int, msg: str) -> None:
    conn = connect(root)
    try:
        conn.execute(
            "UPDATE files SET status='error', error=? WHERE id=?",
            (msg, file_id),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_pending(root: Path, limit: int | None, retry_errors: bool,
                  only_type: str | None) -> list[tuple[int, str, str]]:
    conn = connect(root)
    try:
        statuses = ["pending"]
        if retry_errors:
            statuses.append("error")
        ph = ",".join("?" * len(statuses))
        sql = f"SELECT id, rel_path, type FROM files WHERE status IN ({ph})"
        params: list = list(statuses)
        if only_type:
            sql += " AND type=?"
            params.append(only_type)
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [(r["id"], r["rel_path"], r["type"]) for r in rows]
    finally:
        conn.close()


def reset_done_to_pending(root: Path, only: str | None) -> int:
    conn = connect(root)
    try:
        sql = "UPDATE files SET status='pending', tagged_at=0 WHERE status='done'"
        params: list = []
        if only in ("image", "video"):
            sql += " AND type=?"
            params.append(only)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def build_endpoint_specs(args: argparse.Namespace, llm_cfg: dict) -> list[dict]:
    """Liefert Liste {endpoint, model, api_key, label}.
    Praezedenz: CLI-Endpoint > [[llm.endpoints]] > [llm]."""
    if args.endpoint:
        return [{
            "endpoint": args.endpoint,
            "model":    args.model    or llm_cfg.get("model", "qwen-vl"),
            "api_key":  args.api_key  or llm_cfg.get("api_key", "") or "EMPTY",
            "label":    "cli",
        }]
    multi = llm_cfg.get("endpoints")
    if isinstance(multi, list) and multi:
        disabled = load_disabled_endpoints()
        out = []
        for i, ep in enumerate(multi):
            label = ep.get("label", f"ep{i}")
            if label in disabled:
                continue
            out.append({
                "endpoint": ep.get("endpoint", ""),
                "model":    ep.get("model", llm_cfg.get("model", "")),
                "api_key":  ep.get("api_key", llm_cfg.get("api_key", "")) or "EMPTY",
                "label":    label,
            })
        if not out:
            raise SystemExit("alle Endpoints in settings.json deaktiviert")
        return out
    # Fallback: einzelner [llm]-Block
    return [{
        "endpoint": llm_cfg.get("endpoint", "http://localhost:8000/v1"),
        "model":    args.model   or llm_cfg.get("model", "qwen-vl"),
        "api_key":  args.api_key or llm_cfg.get("api_key", "") or "EMPTY",
        "label":    "ep0",
    }]


def cmd_tag(args: argparse.Namespace) -> None:
    cfg = load_config()
    llm_cfg = cfg.get("llm", {})
    tag_cfg = cfg.get("tag", {})

    endpoints    = build_endpoint_specs(args, llm_cfg)
    workers      = args.workers      if args.workers is not None else int(tag_cfg.get("workers", 4))
    limit        = args.limit        if args.limit   is not None else tag_cfg.get("limit")
    retry_errors = args.retry_errors or bool(tag_cfg.get("retry_errors", False))

    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    init_db(root)

    print(f"tag.py gestartet, root={root}", flush=True)
    if not args.no_scan:
        t_scan = time.time()
        new, changed, moved, removed, total = discover(root)
        print(f"Scan fertig in {time.time()-t_scan:.1f}s "
              f"(total={total} new={new} changed={changed} "
              f"moved={moved} removed={removed})", flush=True)
    if args.scan_only:
        print("scan-only Modus, beende.", flush=True)
        return

    if args.retag:
        n = reset_done_to_pending(root, args.only)
        print(f"Re-Tag: {n} Files auf pending zurueckgesetzt"
              f"{' (nur ' + args.only + ')' if args.only else ''}")

    pending = fetch_pending(root, limit, retry_errors, args.only)
    if not pending:
        print("Nothing to tag.")
        return

    syn = load_synonyms()
    # Pro Endpoint einen OpenAI-Client bauen
    specs = []
    for ep in endpoints:
        specs.append({
            "client":   OpenAI(base_url=ep["endpoint"], api_key=ep["api_key"]),
            "model":    ep["model"],
            "endpoint": ep["endpoint"],
            "label":    ep["label"],
        })
    pool = EndpointPool(specs)
    ep_summary = ", ".join(f"{s['label']}={s['endpoint']}@{s['model']}" for s in specs)
    print(f"Tagging {len(pending)} files via [{ep_summary}] "
          f"(workers={workers}, synonyms={len(syn)} eintraege)")

    done = errors = 0
    total = len(pending)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(tag_one, root, fid, rel, kind, pool, syn)
            for fid, rel, kind in pending
        ]
        ep_done: dict[str, int] = {}
        ep_err:  dict[str, int] = {}
        for i, fut in enumerate(as_completed(futs), 1):
            fid, rel, desc, tags, err, ep_label = fut.result()
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (total - i) / rate if rate > 0 else 0
            rel_safe = _safe(rel)
            if err:
                errors += 1
                ep_err[ep_label] = ep_err.get(ep_label, 0) + 1
                print(f"[{i}/{total}] ERR  [{ep_label}] {rel_safe}  -> {err}", flush=True)
            else:
                done += 1
                ep_done[ep_label] = ep_done.get(ep_label, 0) + 1
                preview = ", ".join(tags[:6]) + ("..." if len(tags) > 6 else "")
                print(f"[{i}/{total}] OK   [{ep_label}] {rel_safe}  | {preview}", flush=True)
            if i % 25 == 0 or i == total:
                pool_stats = pool.stats()
                ep_summary = "  ".join(
                    f"{s['label']}: ok={ep_done.get(s['label'],0)} "
                    f"err={ep_err.get(s['label'],0)} "
                    f"avg={s['avg_s']:.1f}s in_flight={s['in_flight']}"
                    for s in pool_stats
                )
                print(f"  -- progress: done={done} err={errors} "
                      f"rate={rate:.2f}/s eta={eta/60:.1f}min  | {ep_summary} --",
                      flush=True)
    print(f"Finished. done={done} errors={errors} total_time={(time.time()-t0)/60:.1f}min")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Tag media via vision LLM. Defaults aus config.toml; "
                    "CLI-Flags ueberschreiben.")
    p.add_argument("root", help="Root directory of media")
    p.add_argument("--endpoint", default=None, help="OpenAI-compatible base URL")
    p.add_argument("--model", default=None, help="Model name")
    p.add_argument("--api-key", default=None)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--limit", type=int, default=None,
                   help="Max files to tag this run (incremental batches)")
    p.add_argument("--retry-errors", action="store_true",
                   help="Also retry files with status=error")
    p.add_argument("--retag", action="store_true",
                   help="Setzt bereits getaggte Files auf pending zurueck")
    p.add_argument("--only", choices=["image", "video"], default=None,
                   help="Restrict to one media type")
    p.add_argument("--no-scan", action="store_true",
                   help="Skip filesystem rescan")
    p.add_argument("--scan-only", action="store_true",
                   help="Nur Scan ausfuehren, kein LLM-Tagging")
    return p


if __name__ == "__main__":
    cmd_tag(build_parser().parse_args())
