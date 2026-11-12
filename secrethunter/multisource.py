#!/usr/bin/env python3
"""multisource.py — мульти-источниковый поиск секретов БЕЗ токенов.

Интеграция из testsploit/monitor/search_monitor.py + config/sources.json
(там 87 источников). Мы берём только те, что работают из песочницы БЕЗ
токена и БЕЗ прокси — проверено живым запросом 2026-11-12:

  OK 200: GitHub gists, HF datasets/models, StackExchange, HN Algolia, Zenodo,
          npm, PyPI, Docker Hub, GitLab, PublicWWW, Software Heritage
  429/403: grep.app (нужен прокси), Sourcegraph (нужен токен), Codeberg (429)

Зачем: GitHub code-search даёт лишь 10 запросов/мин на аккаунт, а эти
источники свободны и расширяют корпус за пределы GitHub (HF, npm, PyPI, SWH…).

Что взято из testsploit дословно:
  * набор источников и их API-эндпоинты;
  * схема результата (source/query/url/title/snippet/fingerprint);
  * «skip placeholders + min entropy» на этапе извлечения.

CLI:
  python3 multisource.py hunt "sk-or-v1-" [--n 30]   # искать префикс
  python3 multisource.py sources                      # проверить живость источников
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "multisource-findings.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Редкие/нишевые префиксы из testsploit/config/settings.json — то, что обычные
# сканеры не ищут (gemini/openai/anthropic ищут миллионами и всё протухло).
RARE_PREFIXES = [
    "nvapi-", "csk-", "lsv2_pt_", "lsv2_sk_", "esecret_", "sk-helicone-",
    "modal-prod-", "modal-dev-", "modal-main-", "fw_", "v0_", "pk.eyJ",
    "NRII-", "NRAK-", "SG.", "re_", "AKIA", "HRKU-", "msk-", "tgp_v1_",
    "pplx-", "tvly-", "exa-", "fc-", "pcsk_", "sbp_", "wsk_live_", "dop_v1_",
    "ABSK", "nfp_", "r8_", "glpat-", "gsk_", "xai-", "CO.", "hf_",
]

PLACEHOLDER = re.compile(
    r"(xxxx|your[_-]?key|insert|replace|example|placeholder|dummy|test[_-]?key"
    r"|changeme|todo|fixme|secret_here|my[_-]?key|<[^>]+>|\.\.\.)", re.I)


def _fp(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _get(url: str, timeout: float = 20, hdr: dict | None = None):
    h = {"User-Agent": UA, "Accept": "application/json, */*"}
    if hdr:
        h.update(hdr)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:  # noqa: BLE001
            return e.code, b""
    except Exception:  # noqa: BLE001
        return 0, b""


# ── источники (из testsploit, проверены живыми) ──────────────────────────────
def src_hf_datasets(q, n):
    s, b = _get(f"https://huggingface.co/api/datasets?search={urllib.parse.quote(q)}&limit={n}")
    if s != 200:
        return []
    return [{"url": f"https://huggingface.co/datasets/{d['id']}", "title": d["id"]} for d in json.loads(b)]


def src_hf_models(q, n):
    s, b = _get(f"https://huggingface.co/api/models?search={urllib.parse.quote(q)}&limit={n}")
    if s != 200:
        return []
    return [{"url": f"https://huggingface.co/{m['id']}", "title": m["id"]} for m in json.loads(b)]


def src_stackexchange(q, n):
    u = f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={urllib.parse.quote(q)}&site=stackoverflow&pagesize={n}"
    s, b = _get(u)
    if s != 200:
        return []
    return [{"url": i.get("link", ""), "title": i.get("title", "")} for i in json.loads(b).get("items", [])]


def src_hn(q, n):
    s, b = _get(f"https://hn.algolia.com/api/v1/search?query={urllib.parse.quote(q)}&hitsPerPage={n}")
    if s != 200:
        return []
    return [{"url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
             "title": h.get("title") or h.get("story_title") or ""} for h in json.loads(b).get("hits", [])]


def src_zenodo(q, n):
    s, b = _get(f"https://zenodo.org/api/records?q={urllib.parse.quote(q)}&size={n}")
    if s != 200:
        return []
    return [{"url": r.get("links", {}).get("self_html", ""), "title": r.get("metadata", {}).get("title", "")}
            for r in json.loads(b).get("hits", {}).get("hits", [])]


def src_npm(q, n):
    s, b = _get(f"https://registry.npmjs.org/-/v1/search?text={urllib.parse.quote(q)}&size={n}")
    if s != 200:
        return []
    return [{"url": o["package"].get("links", {}).get("repository", ""), "title": o["package"]["name"]}
            for o in json.loads(b).get("objects", [])]


def src_gitlab(q, n):
    s, b = _get(f"https://gitlab.com/api/v4/projects?search={urllib.parse.quote(q)}&per_page={n}")
    if s != 200:
        return []
    return [{"url": p.get("web_url", ""), "title": p.get("path_with_namespace", "")} for p in json.loads(b)]


def src_swh(q, n):
    # SWH отдаёт HTML на не-JSON; принимаем только application/json
    s, b = _get(f"https://archive.softwareheritage.org/api/1/origin/search/{urllib.parse.quote(q)}/?limit={n}",
                hdr={"Accept": "application/json"})
    if s != 200 or not b.strip().startswith(b"["):
        return []
    return [{"url": o.get("url", ""), "title": o.get("url", "")} for o in json.loads(b)]


def src_publicwww(q, n):
    s, b = _get(f"https://publicwww.com/websites/%22{urllib.parse.quote(q)}%22/")
    if s != 200:
        return []
    return [{"url": f"https://publicwww.com/websites/%22{q}%22/", "title": f"PublicWWW: {q} ({len(b)}b)"}]


def src_dockerhub(q, n):
    s, b = _get(f"https://hub.docker.com/v2/search/repositories/?query={urllib.parse.quote(q)}&page_size={n}")
    if s != 200:
        return []
    return [{"url": f"https://hub.docker.com/r/{r.get('repo_name','')}", "title": r.get("repo_name", "")}
            for r in json.loads(b).get("results", [])]


SOURCES = {
    "hf_datasets": src_hf_datasets, "hf_models": src_hf_models,
    "stackexchange": src_stackexchange, "hn_algolia": src_hn,
    "zenodo": src_zenodo, "npm": src_npm, "gitlab": src_gitlab,
    "swh": src_swh, "publicwww": src_publicwww, "dockerhub": src_dockerhub,
}


def hunt(prefix: str, n: int = 30) -> list[dict]:
    """Искать префикс по всем свободным источникам. Возврат — находки-цели."""
    out, seen = [], set()
    for name, fn in SOURCES.items():
        try:
            for r in fn(prefix, n):
                u = r.get("url", "")
                if not u or u in seen:
                    continue
                seen.add(u)
                out.append({"source": name, "query": prefix, "url": u,
                            "title": r.get("title", ""), "fingerprint": _fp(u)})
        except Exception as e:  # noqa: BLE001
            print(f"  [{name}] ошибка {type(e).__name__}: {str(e)[:60]}")
    return out


def check_sources() -> None:
    for name, fn in SOURCES.items():
        t = time.time()
        try:
            r = fn("openai", 2)
            print(f"{'OK ' if r else '   '} {name:14} {len(r)} результатов  {time.time()-t:.1f}s")
        except Exception as e:  # noqa: BLE001
            print(f"ERR {name:14} {type(e).__name__}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sources"
    if cmd == "sources":
        check_sources()
    elif cmd == "hunt":
        q = sys.argv[2] if len(sys.argv) > 2 else "sk-or-v1-"
        n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 30
        res = hunt(q, n)
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(f"[multisource] '{q}': {len(res)} целей -> {OUT.name}")
        for r in res[:12]:
            print(f"  {r['source']:14} {r['title'][:60]}")
