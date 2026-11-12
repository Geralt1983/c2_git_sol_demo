#!/usr/bin/env python3
"""classify.py — «разборка» неизвестного ключа: что это за сервис и что он умеет.

Задача: в чужом файле лежит секрет с непонятным именем (`API_KEY=...`, `TOKEN=...`).
Мы не знаем, чей он. Решение — не гадать, а СПРОСИТЬ у сервисов: по очереди
предъявить значение всем дешёвым чекерам (281 чекер в checkers.py) и посмотреть,
кто ответит 200.

Порядок проб — по подсказкам:
  1. префикс значения (sk-or-v1- -> openrouter, ghp_ -> github, hf_ -> huggingface…);
  2. имя переменной, если известно;
  3. остальные чекеры, отсортированные «сначала дешёвые GET /models и /user».

Для найденного сервиса подтягиваем возможности из каталога models.dev
(cat: text/video/audio/image/embed/pdf/agent/reasoning) — то есть отвечаем
не «ключ жив», а «этот ключ умеет: видео, аудио, агенты».

Запуск:
  python3 tools/classify.py value sk-ant-api03-xxxx
  python3 tools/classify.py file tools/autopipeline_raw.json   # разобрать все находки
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import checkers as C  # noqa: E402

OUT = ROOT / "classify-results.json"

# дешёвые «отпечатки»: один GET, без тела — их безопасно предъявлять первыми
CHEAP_FIRST = [
    "github", "gitlab-pat", "huggingface-token", "openrouter", "openai", "anthropic",
    "groq", "deepseek", "mistral", "together", "cohere", "xai", "perplexity",
    "tavily", "exa", "firecrawl", "jina", "elevenlabs", "replicate", "airtable",
    "notion", "slack", "stripe", "mapbox", "telegram", "supabase-pat", "vercel-token",
    "netlify", "deno", "render", "fly", "koyeb", "railway", "neon", "appwrite",
    "valtown", "pipedream", "northflank", "cloudflare-token", "digitalocean",
    "codeberg", "pythonanywhere", "sambanova", "nvidia", "fal", "wavespeed",
    "minimax", "cerebras", "dashscope", "moonshot", "zhipu", "aimlapi", "novita",
    "venice", "ofox", "kilo", "zenmux", "abacus", "ollama-cloud", "requesty",
    "llmgateway", "edenai", "kie", "apimart", "deepinfra", "baseten", "anyscale",
]

PREFIX_HINT = [
    ("sk-or-v1-", "openrouter"), ("sk-ant-", "anthropic"), ("gsk_", "groq"),
    ("hf_", "huggingface-token"), ("ghp_", "github"), ("gho_", "github"),
    ("github_pat_", "github"), ("glpat-", "gitlab-pat"), ("r8_", "replicate"),
    ("xai-", "xai"), ("AIza", "google"), ("pplx-", "perplexity"), ("tvly-", "tavily"),
    ("exa-", "exa"), ("fc-", "firecrawl"), ("wsk_live_", "wavespeed"),
    ("sk-cp-", "minimax"), ("csk-", "cerebras"), ("sk-dash", "dashscope"),
    ("pcsk_", "pinecone"), ("sntrys_", "sentry"), ("sbp_", "supabase-pat"),
    ("pat", "airtable"), ("ntn_", "notion"), ("nfp_", "netlify"),
    ("dop_v1_", "digitalocean"), ("ddp_", "deno"), ("FlyV1", "fly"),
    ("key_", "pipedream"), ("sk-", "openai"), ("sk_", "openai"),
]

# что умеет сервис — из каталога models.dev (реальная разборка возможностей API).
# Приоритет: точные возможности из models.dev (видео/аудио/агент/PDF), иначе — наша метка.
CAPS = {}
_gen = ROOT / "checkers_generated.json"
if _gen.exists():
    for pid, spec in json.loads(_gen.read_text()).get("checkers", {}).items():
        CAPS[pid] = spec.get("cat", "text")
_caps_report = ROOT / "capabilities-report.json"
if _caps_report.exists():
    for row in json.loads(_caps_report.read_text()).get("rows", []):
        if row.get("caps"):
            CAPS[row["service"]] = "/".join(row["caps"])


def order(value: str, name: str = "") -> list[str]:
    """Порядок проб: подсказки -> дешёвые -> остальные."""
    head = []
    for pre, svc in PREFIX_HINT:
        if value.startswith(pre) and svc in C.CHECKS:
            head.append(svc)
            break
    if name:
        for cand in (name, name.upper(), name.replace("_API_KEY", "").lower()):
            if cand in C.CHECKS and cand not in head:
                head.append(cand)
    seq = head + [s for s in CHEAP_FIRST if s in C.CHECKS and s not in head]
    seq += [s for s in C.CHECKS if s not in seq]
    return seq


def probe(svc: str, value: str, timeout: float = 8) -> dict | None:
    try:
        r = C.check_one(svc, value, None, None, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if r.get("status") in ("alive", "weak-alive", "valid-no-credits"):
        return {"service": svc, "status": r["status"], "http": r.get("http"),
                "deep_http": r.get("deep_http"), "cat": CAPS.get(svc, C.CHECKS[svc]["cat"]),
                "body": str(r.get("body"))[:160]}
    return None


def classify(value: str, name: str = "", limit: int = 60, workers: int = 10) -> dict:
    seq = order(value, name)[:limit]
    found = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(lambda s: probe(s, value), seq):
            if r:
                found.append(r)
    caps = sorted({c for r in found for c in r["cat"].split("/")})
    out = {"value": value[:14] + "…", "name": name, "tried": len(seq),
           "services": found, "capabilities": caps,
           "verdict": ("ключ опознан: " + ", ".join(r["service"] for r in found)) if found
                      else "не опознан (или сервиса нет в реестре 281)"}
    return out


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "value"
    if cmd == "value":
        res = classify(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif cmd == "file":
        items = json.loads(Path(sys.argv[2]).read_text())
        results = []
        for h in items[:40]:
            v = h.get("value")
            if not v:
                continue
            r = classify(v, h.get("name") or "")
            r["repo"] = h.get("repo")
            results.append(r)
            print(f"{r['value']:<18} {r['verdict'][:80]}")
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print(f"сохранено: {OUT}")


if __name__ == "__main__":
    main()
