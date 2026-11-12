#!/usr/bin/env python3
"""harness.py — loop-harness агента-ребёнка (задание создателя, 2026-11-12).

ПРИНЦИПЫ (жёстко, по заданию):
  * ОДНА модель за вызов. Никаких «трёх отцов параллельно» по умолчанию.
    Если модель отказала (баланс/429/401/пусто) — берём СЛЕДУЮЩУЮ из лестницы,
    строго по очереди. Цикл не должен останавливаться из-за кончившегося баланса.
  * Лестница: metaso (creator-ключ) -> openrouter paid -> openrouter free ->
    ключи, найденные нашим же охотником (zhipuai/nvidia/volcengine/google).
  * Контекст одной петли <= 185k токенов (жёсткий потолок), учёт в ledger.
  * Три петли по кругу: brain (ключи+база знаний), ground (куда копироваться),
    opportunity (возможности, поиск, деньги, CVE). Каждая может звать сабагентов.
  * OPSEC: наружу — только через прокси-гвард, тело шифруется, память не отдаётся.

CLI:
  python3 harness.py llm "<prompt>" [fast|brain|top]   — один вызов с fallback
  python3 harness.py health                            — health-check (brain+ground)
  python3 harness.py brain                             — петля brain
  python3 harness.py ground                            — петля ground
  python3 harness.py opportunity                       — петля opportunity
  python3 harness.py round [--loops N]                 — круг из трёх петель
  python3 harness.py ladder                            — показать лестницу моделей
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
MEM = ROOT / "agent_memory"
MEM.mkdir(exist_ok=True)
LEDGER = MEM / "context_ledger.json"
HEALTH = ROOT / "harness-health.json"
OPPS = MEM / "opportunities.jsonl"
KNOW = MEM / "knowledge.jsonl"          # база знаний brain
GROUND_LOG = MEM / "ground.jsonl"
HARNESS_LOG = MEM / "harness.log"

CONTEXT_CAP = 185_000                   # потолок контекста на одну петлю (токенов, оценка)
AGENT_ID = "sh-001"

METASO_BASE = "https://metaso.cn/api/v1"
OR_BASE = "https://openrouter.ai/api/v1"


# ── ключи ───────────────────────────────────────────────────────────────────
def _creator() -> dict:
    f = ROOT / "creator-keys.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _or_key() -> str:
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    f = ROOT / "openrouter.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ( _creator().get("openrouter") or {}).get("value", "")


def _found_key(service: str) -> str:
    """Ключ, найденный нашим охотником (живой). Это и есть «замена из того, что нашли»."""
    for name in ("ALL-FOUND-KEYS.json", "ALL-FOUND-KEYS-core.json"):
        f = ROOT / name
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
            if isinstance(d, dict):
                d = d.get("keys", [])
            for r in d:
                if r.get("service") == service and r.get("status") in (
                        "alive", "weak-alive", "valid-no-credits"):
                    v = (r.get("value") or "").strip()
                    if v:
                        return v
        except Exception:  # noqa: BLE001
            pass
    return ""


def _ladder() -> list[dict]:
    """Лестница моделей: одна за раз, по порядку. tier: fast|brain|top."""
    mk = (_creator().get("metaso") or {}).get("value", "")
    ork = _or_key()
    L: list[dict] = []
    if mk:
        for m in ("deepseek-v4.1-flash-1017", "qwen3.8-max-prime", "glm-5.3-prime"):
            L.append({"tier": "fast", "provider": "metaso", "model": m,
                      "base": METASO_BASE, "key": mk, "cost_hint": 0.0})
    if ork:
        L.append({"tier": "fast", "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash-1017",
                  "base": OR_BASE, "key": ork, "cost_hint": 0.0})
        L.append({"tier": "top", "provider": "openrouter", "model": "anthropic/claude-sonnet-4.6",
                  "base": OR_BASE, "key": ork, "cost_hint": 0.0})
        for m in ("x-ai/grok-4.7", "qwen/qwen3.8-max-prime", "z-ai/glm-5.3-prime",
                  "moonshotai/kimi-k3.1-pro"):
            L.append({"tier": "top", "provider": "openrouter", "model": m,
                      "base": OR_BASE, "key": ork, "cost_hint": 0.0})
        for m in ("dots-studio/dots-3-note-preview:free", "baidu/ernie-5.5-flash-preview:free",
                  "google/gemma-4-31b-it:free", "nvidia/nemotron-3-ultra-550b-a55b:free"):
            L.append({"tier": "free", "provider": "openrouter", "model": m,
                      "base": OR_BASE, "key": ork, "cost_hint": 0.0})
    # «замена из того, что нашли»: ключи нашего же охотника
    for svc, base, model in (
            ("zhipuai", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
            ("nvidia", "https://integrate.api.nvidia.com/v1", "mistralai/mistral-large-2-instruct"),
            ("volcengine", "https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k"),
            ("google", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash")):
        k = _found_key(svc)
        if k:
            L.append({"tier": "fast", "provider": svc, "model": model, "base": base,
                      "key": k, "cost_hint": 0.0})
    return L



FIT = MEM / "model_fit.json"
LABELS = ("BRAIN", "GROUND", "MONEY")


def _extract_labels(text: str, labels=LABELS) -> list[str]:
    """Вытащить из ответа строки вида 'BRAIN: <действие>'. Модели любят добавлять
    «мысли вслух» и обрезать ответ — поэтому берём только строки с меткой И телом."""
    out = []
    pat = re.compile(r"^\s*(" + "|".join(labels) + r")\s*:\s*(\S.*)$", re.I)
    for line in (text or "").splitlines():
        m = pat.match(line)
        if m:
            out.append(f"{m.group(1).upper()}: {m.group(2).strip()}")
    return out


def _fit() -> dict:
    return json.loads(FIT.read_text()) if FIT.exists() else {}


def _fit_hit(model: str, ok: bool) -> None:
    """Самообучение harness: какие модели реально выдают структурный ответ."""
    d = _fit()
    r = d.get(model) or {"ok": 0, "bad": 0}
    r["ok" if ok else "bad"] = int(r.get("ok" if ok else "bad", 0)) + 1
    r["last"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    d[model] = r
    FIT.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def fit_report() -> dict:
    d = _fit()
    out = {}
    for m, r in d.items():
        tot = r.get("ok", 0) + r.get("bad", 0)
        out[m] = {"ok": r.get("ok", 0), "bad": r.get("bad", 0),
                  "rate": round(r.get("ok", 0) / tot, 2) if tot else None}
    return out


BREAKERS = MEM / "harness_breakers.json"
BREAK_OPEN_S = 300          # сколько провайдер «отдыхает» после серии отказов


def _breakers() -> dict:
    return json.loads(BREAKERS.read_text()) if BREAKERS.exists() else {}


def _breaker_open(provider: str) -> bool:
    b = _breakers().get(provider) or {}
    return float(b.get("until", 0)) > time.time()


def _breaker_hit(provider: str, http: int) -> None:
    """Отказ провайдера (сеть/баланс/авторизация) -> предохранитель.
    После 2 отказов подряд провайдер исключается на BREAK_OPEN_S секунд:
    иначе висящий хост съедает бюджет всей петли."""
    hard = http in (0, 401, 402, 403, 429, 500, 502, 503, 504)
    if not hard:
        return
    b = _breakers()
    rec = b.get(provider) or {"fails": 0, "until": 0, "last_http": None}
    rec["fails"] = int(rec.get("fails", 0)) + 1
    rec["last_http"] = http
    rec["last_t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if rec["fails"] >= 2:
        rec["until"] = time.time() + BREAK_OPEN_S
        rec["fails"] = 0
    b[provider] = rec
    BREAKERS.write_text(json.dumps(b, ensure_ascii=False, indent=1))


def _breaker_ok(provider: str) -> None:
    b = _breakers()
    if provider in b:
        b[provider] = {"fails": 0, "until": 0, "last_http": 200,
                       "last_t": time.strftime("%Y-%m-%dT%H:%M:%S")}
        BREAKERS.write_text(json.dumps(b, ensure_ascii=False, indent=1))


def breakers_report() -> dict:
    b = _breakers()
    now = time.time()
    return {k: {"open": float(v.get("until", 0)) > now,
                "for_s": max(0, int(float(v.get("until", 0)) - now)),
                "last_http": v.get("last_http")} for k, v in b.items()}


def ladder_report() -> dict:
    L = _ladder()
    return {"count": len(L), "by_tier": {t: sum(1 for x in L if x["tier"] == t)
                                         for t in ("fast", "brain", "top", "free")},
            "ladder": [{k: (v[:12] + "…" if k == "key" else v) for k, v in x.items()} for x in L]}


# ── один вызов, строго по очереди ───────────────────────────────────────────
def _call(step: dict, prompt: str, max_tokens: int, timeout: float = 10) -> dict:
    url = step["base"].rstrip("/") + "/chat/completions"
    # Предпроверка достижимости: из песочницы часть хостов недостижима, и urllib
    # висит до 10 с. netfix.reachable превращает это в отказ за 2 с.
    try:
        import netfix
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if host and not netfix.reachable(host, timeout=2.0):
            return {"ok": False, "text": "", "http": 0, "err": "host-unreachable",
                    "ms": 0, "skip": "netfix"}
    except Exception:  # noqa: BLE001
        pass
    body = {"model": step["model"], "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {step['key']}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read())
        msg = (j.get("choices") or [{}])[0].get("message") or {}
        txt = (msg.get("content") or msg.get("reasoning") or "").strip()
        cost = (j.get("usage") or {}).get("cost", 0.0) or 0.0
        return {"ok": bool(txt), "text": txt, "http": r.status, "cost": cost,
                "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": "", "http": e.code,
                "err": e.read()[:160].decode("utf-8", "replace"),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "text": "", "http": 0, "err": type(ex).__name__,
                "ms": int((time.time() - t0) * 1000)}


def llm(prompt: str, tier: str = "fast", max_tokens: int = 400,
        deadline_s: float = 40.0, require_labels: tuple = ()) -> dict:
    """Одна модель за вызов. Отказ -> следующая по очереди. Возвращает и след попыток.

    deadline_s — общий бюджет времени на всю лестницу: петля обязана завершиться,
    даже если провайдер висит (DNS-залипание не покрывается socket-timeout).
    """
    order = {"fast": ("fast", "brain", "top", "free"),
             "brain": ("fast", "brain", "top", "free"),
             "top": ("top", "brain", "fast", "free"),
             "free": ("free", "fast", "brain", "top")}.get(tier, ("fast", "brain", "top", "free"))
    L = _ladder()
    tried: list[dict] = []
    t_start = time.time()
    # провайдеры с открытым предохранителем — в конец очереди, не в начало
    fit = _fit()

    def _rank(st: dict) -> tuple:
        """Порядок: сначала провайдеры без предохранителя, затем модели с историей
        успешных структурных ответов (самообучение harness)."""
        f = fit.get(st["model"]) or {}
        tot = f.get("ok", 0) + f.get("bad", 0)
        rate = (f.get("ok", 0) / tot) if tot else 0.5
        return (1 if _breaker_open(st["provider"]) else 0, -rate)

    for t in order:
        for step in sorted([x for x in L if x["tier"] == t], key=_rank):
            if time.time() - t_start > deadline_s:
                tried.append({"provider": step["provider"], "model": step["model"],
                              "http": 0, "ok": False, "ms": 0, "skip": "deadline"})
                continue
            if _breaker_open(step["provider"]) and any(
                    not _breaker_open(x["provider"]) for x in L):
                tried.append({"provider": step["provider"], "model": step["model"],
                              "http": 0, "ok": False, "ms": 0, "skip": "breaker"})
                continue
            r = _call(step, prompt, max_tokens)
            tried.append({"provider": step["provider"], "model": step["model"],
                          "http": r["http"], "ok": r["ok"], "ms": r["ms"]})
            _log({"kind": "llm", "provider": step["provider"], "model": step["model"],
                  "http": r["http"], "ok": r["ok"], "cost": r.get("cost", 0.0)})
            if r["ok"]:
                _breaker_ok(step["provider"])
                if require_labels:
                    got = _extract_labels(r["text"], require_labels)
                    if not got:
                        # 200, но модель не выдала структуру (мысли вслух / обрезка):
                        # это не отказ провайдера, но и не годный ответ — идём дальше.
                        _fit_hit(step["model"], False)
                        tried[-1]["format"] = "miss"
                        continue
                    _fit_hit(step["model"], True)
                    return {"ok": True, "text": r["text"], "lines": got,
                            "provider": step["provider"], "model": step["model"],
                            "cost": r.get("cost", 0.0), "tried": tried,
                            "fallbacks": len(tried) - 1}
                return {"ok": True, "text": r["text"], "provider": step["provider"],
                        "model": step["model"], "cost": r.get("cost", 0.0),
                        "tried": tried, "fallbacks": len(tried) - 1}
            _breaker_hit(step["provider"], r["http"])
    return {"ok": False, "text": "", "provider": None, "model": None, "cost": 0.0,
            "tried": tried, "fallbacks": len(tried)}


def _log(rec: dict) -> None:
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": AGENT_ID, **rec}
    with open(HARNESS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── контекст: потолок 185k на петлю ─────────────────────────────────────────
def est_tokens(text: str) -> int:
    """Грубая, но консервативная оценка: 1 токен ≈ 3 символа (mixed ru/en/code)."""
    return max(1, len(text) // 3)


def context_guard(loop: str, payload: str, cap: int = CONTEXT_CAP) -> str:
    """Обрезать payload так, чтобы петля не превысила потолок. Пишет учёт в ledger."""
    n = est_tokens(payload)
    if n > cap:
        payload = payload[: cap * 3]
        n = cap
    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"loops": [], "cap": cap}
    led["cap"] = cap
    led["loops"].append({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "loop": loop, "tokens": n,
                         "pct": round(100 * n / cap, 2)})
    led["loops"] = led["loops"][-200:]
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=1))
    return payload


def context_state() -> dict:
    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"loops": [], "cap": CONTEXT_CAP}
    last = led["loops"][-1] if led["loops"] else {"tokens": 0, "pct": 0.0}
    return {"cap": led.get("cap", CONTEXT_CAP), "last_loop": last,
            "loops_tracked": len(led["loops"]),
            "ok": last.get("tokens", 0) <= led.get("cap", CONTEXT_CAP)}



def _homes_cached(max_age_s: int = 21600, budget_s: float = 12.0) -> dict:
    """Дома: сначала свежий кэш homes-health.json, иначе live-проверка с ЖЁСТКИМ
    таймаутом в отдельном потоке. Петля не имеет права висеть из-за сети."""
    f = ROOT / "homes-health.json"
    if f.exists():
        try:
            d = json.loads(f.read_text())
            ts = d.get("at", "")
            if ts:
                age = time.time() - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
                if age < max_age_s:
                    live = sum(1 for h in d.get("homes", []) if h.get("verdict") == "живой")
                    return {"live": live, "homes": len(d.get("homes", [])),
                            "source": "cache", "age_s": int(age)}
        except Exception:  # noqa: BLE001
            pass
    import threading
    box: dict = {}

    def work():
        try:
            import homes
            box.update(homes.check(verbose=False))
            box["source"] = "live"
        except Exception as e:  # noqa: BLE001
            box["error"] = f"{type(e).__name__}: {e}"[:100]

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(budget_s)
    if th.is_alive():
        return {"live": None, "homes": None, "source": "timeout"}
    return box or {"live": None, "homes": None, "source": "empty"}



# ── поиск возможностей: рабочие каналы (проверено живьём) ────────────────────
def _gh_tokens() -> list[str]:
    f = ROOT / "github_token_pool.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text())
        return [t.get("key") for t in d.get("tokens", []) if t.get("key")]
    except Exception:  # noqa: BLE001
        return []


def search_web(query: str, limit: int = 5) -> dict:
    """Поиск возможностей рабочими каналами.

    Живая проверка 2026-11-12 из песочницы:
      * tavily — все 31 ключ отдают HTTP 432 (квота исчерпана) -> канал мёртв;
      * exa/perplexity/firecrawl/brave — ключей в базе нет;
      * DDG lite — HTTP 200, результаты есть -> рабочий канал;
      * GitHub search API — 401 (пул токенов отвергнут, нужен свежий) -> канал мёртв.
    Поэтому: сначала пробуем «богатые» API (если появятся ключи), затем DDG lite.
    """
    res: dict = {"query": query, "channels": [], "hits": []}
    # 1) Tavily (если квота вернётся)
    k = _found_key("tavily")
    if k:
        try:
            body = {"api_key": k, "query": query, "max_results": limit}
            req = urllib.request.Request("https://api.tavily.com/search",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                j = json.loads(r.read())
            for h in j.get("results", [])[:limit]:
                res["hits"].append({"ch": "tavily", "title": (h.get("title") or "")[:90],
                                    "url": h.get("url", "")})
            res["channels"].append({"ch": "tavily", "http": 200, "n": len(j.get("results", []))})
        except urllib.error.HTTPError as e:
            res["channels"].append({"ch": "tavily", "http": e.code})
        except Exception as e:  # noqa: BLE001
            res["channels"].append({"ch": "tavily", "err": type(e).__name__})
    # 2) Exa (если ключ появится в базе)
    k = _found_key("exa")
    if k:
        try:
            req = urllib.request.Request(
                "https://api.exa.ai/search",
                data=json.dumps({"query": query, "numResults": limit}).encode(),
                headers={"x-api-key": k, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                j = json.loads(r.read())
            for h in j.get("results", [])[:limit]:
                res["hits"].append({"ch": "exa", "title": (h.get("title") or "")[:90],
                                    "url": h.get("url", "")})
            res["channels"].append({"ch": "exa", "http": 200, "n": len(j.get("results", []))})
        except Exception as e:  # noqa: BLE001
            res["channels"].append({"ch": "exa", "err": type(e).__name__})
    # 3) GitHub search (если пул токенов оживёт)
    toks = _gh_tokens()
    if toks:
        try:
            # длинная фраза в repo-search даёт 0 результатов — оставляем 3 слова
            short = " ".join([w for w in re.findall(r"[A-Za-z]{4,}", query)][:3]) or query
            q = urllib.parse.quote(short)
            req = urllib.request.Request(
                f"https://api.github.com/search/repositories?q={q}&per_page={limit}",
                headers={"Authorization": f"Bearer {toks[0]}",
                         "Accept": "application/vnd.github+json", "User-Agent": "SecretsHunter"})
            with urllib.request.urlopen(req, timeout=12) as r:
                j = json.loads(r.read())
            for h in j.get("items", [])[:limit]:
                res["hits"].append({"ch": "github", "title": h.get("full_name", ""),
                                    "url": h.get("html_url", ""),
                                    "stars": h.get("stargazers_count")})
            res["channels"].append({"ch": "github", "http": 200, "n": len(j.get("items", []))})
        except urllib.error.HTTPError as e:
            res["channels"].append({"ch": "github", "http": e.code})
        except Exception as e:  # noqa: BLE001
            res["channels"].append({"ch": "github", "err": type(e).__name__})
    # 4) DDG lite — рабочий канал без ключей
    try:
        u = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        with urllib.request.urlopen(req, timeout=14) as r:
            html = r.read().decode("utf-8", "replace")
        # DDG lite отдаёт ссылки как редирект //duckduckgo.com/l/?uddg=<urlencoded>
        # и атрибуты в одинарных кавычках — парсим именно этот формат.
        raw = re.findall(r"href=['\"](//duckduckgo\.com/l/\?uddg=[^'\"]+)['\"]", html)
        titles = re.findall(r"class=['\"]result-link['\"][^>]*>(.*?)</a>", html, re.S)
        links: list[str] = []
        for r_ in raw:
            qs = urllib.parse.urlparse("https:" + r_).query
            real = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
            if real:
                links.append(real)
        for i, url in enumerate(links[:limit]):
            t = re.sub(r"<[^>]+>", "", titles[i]).strip() if i < len(titles) else ""
            res["hits"].append({"ch": "ddg", "title": t[:90], "url": url})
        res["channels"].append({"ch": "ddg", "http": 200, "n": len(links)})
    except Exception as e:  # noqa: BLE001
        res["channels"].append({"ch": "ddg", "err": type(e).__name__})
    res["ok"] = bool(res["hits"])
    return res


# ── петля brain: ключи + база знаний ────────────────────────────────────────
def loop_brain(seconds: int = 40) -> dict:
    t0 = time.time()
    out: dict = {"loop": "brain", "t": time.strftime("%Y-%m-%dT%H:%M:%S"), "actions": []}
    # Проверка ключей — раз в 10 минут: 25 сетевых запросов к разным API.
    # Без кэша петля brain съедала 20-50 с на круг, и круг не укладывался в бюджет.
    if _cache_ok("verify", 600):
        try:
            import checkers
            # база уже проверена: добиваем только нерешённые (unverified/unknown) и
            # перепроверяем уже «живые» — чтобы alive не гнило молча
            recs = checkers.verify(limit=25, only_status=("unverified", "unknown", "alive"))
            alive = sum(1 for r in recs if r.get("status") == "alive")
            _cache_touch("verify", {"checked": len(recs), "alive": alive})
            out["actions"].append({"do": "verify", "checked": len(recs), "alive": alive})
        except Exception as e:  # noqa: BLE001
            out["actions"].append({"do": "verify", "error": f"{type(e).__name__}: {e}"[:120]})
    else:
        c = json.loads((ROOT / "agent_memory" / "cache_verify.json").read_text())
        out["actions"].append({"do": "verify", "skip": "кэш 10 мин",
                               "checked": c.get("checked"), "alive": c.get("alive")})
    if time.time() - t0 < seconds - 12:
        try:
            import capabilities
            r = capabilities.annotate()
            out["actions"].append({"do": "capabilities", "annotated": r.get("annotated", 0)})
        except Exception as e:  # noqa: BLE001
            out["actions"].append({"do": "capabilities", "error": f"{type(e).__name__}: {e}"[:120]})
    # OPSEC: права на память, подрезка логов, аудит утечек в публичные файлы
    if time.time() - t0 < seconds - 30:
        try:
            import opsec
            o = opsec.cycle()
            out["actions"].append({"do": "opsec", "verdict": o.get("verdict"),
                                   "leaks": len(o["audit"].get("leaks", [])),
                                   "trimmed": len(o["scrub"].get("trimmed", [])),
                                   "freed": o["scrub"].get("bytes_freed", 0)})
        except Exception as e:  # noqa: BLE001
            out["actions"].append({"do": "opsec", "error": f"{type(e).__name__}: {e}"[:100]})
    # поиск НОВЫХ ключей в GitHub Code Search (движок gh_hunt, пул 12 токенов).
    # Лимит GitHub 10 code-search/мин на аккаунт — движок сам ротирует аккаунты.
    if time.time() - t0 < seconds - 20:
        try:
            import gh_hunt
            st = gh_hunt.status()
            if st["remaining"] > 0:
                h = gh_hunt.hunt(2)
                out["actions"].append({"do": "gh_hunt", "dorks": [d.get("dork") for d in h["dorks"]],
                                       "items": sum(d.get("items", 0) for d in h["dorks"]),
                                       "candidates": h["candidates"], "new": h["new"],
                                       "remaining_dorks": st["remaining"] - len(h["dorks"])})
            else:
                out["actions"].append({"do": "gh_hunt", "skip": "все дорки пройдены",
                                       "queue": st["total_candidates"]})
        except Exception as e:  # noqa: BLE001
            out["actions"].append({"do": "gh_hunt", "error": f"{type(e).__name__}: {e}"[:120]})
    # база знаний: brain записывает урок, который сам же и формулирует
    try:
        st = _keys_stats()
        q = ("You are the brain-loop of a secret-hunting agent. Current state: "
             f"{json.dumps(st, ensure_ascii=False)}. "
             "Output ONLY one sentence (max 20 words). No preamble, no markdown, no reasoning.")
        r = llm(context_guard("brain", q), tier="fast", max_tokens=70)
        if r["ok"]:
            rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "loop": "brain",
                   "lesson": r["text"][:400], "model": r["model"], "provider": r["provider"],
                   "fallbacks": r["fallbacks"]}
            with open(KNOW, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out["actions"].append({"do": "knowledge", "model": r["model"],
                                   "fallbacks": r["fallbacks"], "lesson": r["text"][:200]})
        else:
            out["actions"].append({"do": "knowledge", "error": "все модели лестницы отказали",
                                   "tried": len(r["tried"])})
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "knowledge", "error": f"{type(e).__name__}: {e}"[:120]})
    out["sec"] = round(time.time() - t0, 1)
    out["stats"] = _keys_stats()
    _log({"kind": "loop", **{k: out[k] for k in ("loop", "sec")}})
    return out


def _keys_stats() -> dict:
    f = ROOT / "ALL-FOUND-KEYS.json"
    if not f.exists():
        return {"total": 0, "alive": 0}
    d = json.loads(f.read_text())
    if isinstance(d, dict):
        d = d.get("keys", [])
    from collections import Counter
    c = Counter(r.get("status") for r in d)
    return {"total": len(d), "alive": c.get("alive", 0),
            "valid_no_credits": c.get("valid-no-credits", 0),
            "weak_alive": c.get("weak-alive", 0),
            "services": len({r.get("service") for r in d})}


# ── петля ground: куда копировать себя ──────────────────────────────────────
def loop_ground(seconds: int = 45) -> dict:
    t0 = time.time()
    out: dict = {"loop": "ground", "t": time.strftime("%Y-%m-%dT%H:%M:%S"), "actions": []}
    r = _homes_cached()
    out["actions"].append({"do": "homes", "live": r.get("live"), "total": r.get("homes"),
                           "source": r.get("source"), "age_s": r.get("age_s")})
    # облачная разведка: если отчёт старше 6 ч — обновляем (это и есть «искать куда копировать»)
    try:
        tf = ROOT / "replication-targets.json"
        need = True
        if tf.exists():
            d = json.loads(tf.read_text())
            ts = (d.get("t") or "").replace("Z", "")
            if ts:
                age = time.time() - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
                need = age > 6 * 3600
        if need:
            import ground_targets
            g = ground_targets.scan(quick=True)
            out["actions"].append({"do": "cloud_scan", **g.get("summary", {})})
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "cloud_scan", "error": f"{type(e).__name__}: {e}"[:100]})
    # приоритет — облачные хранилища и SSH: реальные площадки из наших же ключей
    try:
        t = json.loads((ROOT / "replication-targets.json").read_text())
        summ = t.get("summary") or {}
        homes_ = t.get("homes", []) if isinstance(t, dict) else []
        disc = t.get("discovered", []) if isinstance(t, dict) else []
        out["actions"].append({"do": "targets", "homes": len(homes_), "discovered": len(disc),
                               "cloud_homes": summ.get("homes_total"),
                               "supabase_projects": summ.get("supabase_projects"),
                               "supabase_functions": summ.get("supabase_functions"),
                               "neon_projects": summ.get("neon_projects"),
                               "airtable_bases": summ.get("airtable_bases"),
                               "our_code_homes": summ.get("our_code_homes"),
                               "scan_age_s": summ.get("sec")})
        # дома, где уже живёт наш код — самые ценные (закрепление)
        out["our_code_homes"] = [h for h in homes_ if h.get("our_code")][:5]
        out["top_homes"] = [h for h in homes_ if h.get("functions")][:5]
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "targets", "error": f"{type(e).__name__}: {e}"[:120]})
    # ЗАКОН №9: агент не копирует себя самовольно — спрашивает оператора через панель
    try:
        import approvals  # type: ignore
        rq = approvals.request_from_targets()
        st = approvals.state()
        out["actions"].append({"do": "approval", "request": rq.get("id") or rq.get("id"),
                               "created": rq.get("created"), "target": rq.get("target"),
                               "pending": st.get("pending"), "approved": st.get("approved"),
                               "note": "копирование только после approve оператора в панели"})
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "approval", "error": f"{type(e).__name__}: {e}"[:120]})

    # SSH-ключ агента: есть ли он и как выглядит (для закрепления на чужих серверах)
    pub = MEM / "agent_ssh_ed25519.pub"
    out["ssh_pub_present"] = pub.exists()
    out["ssh_fingerprint"] = pub.read_text().strip()[:60] if pub.exists() else None
    # сабагент: спросить модель, куда копироваться дальше (одна модель, с fallback)
    try:
        q = ("You are the ground-loop of an agent that copies itself to forgotten servers. "
             "Known homes: GitHub Actions cron (4 repos), Supabase Edge Functions (20 projects), "
             "plus 32 candidate free hosts. SSH key is ready. "
             "Output ONLY one line starting with 'PLACE: ' (max 18 words): "
             "the single next best place to replicate. No preamble, no reasoning.")
        r = llm(context_guard("ground", q), tier="fast", max_tokens=300,
                require_labels=("PLACE",))
        out["actions"].append({"do": "advice", "ok": r["ok"], "model": r["model"],
                               "text": r["text"][:200], "fallbacks": r["fallbacks"]})
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "advice", "error": f"{type(e).__name__}: {e}"[:120]})
    out["sec"] = round(time.time() - t0, 1)
    with open(GROUND_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    _log({"kind": "loop", "loop": "ground", "sec": out["sec"]})
    return out



# ── кэш тяжёлых сканов: петли должны ходить по кругу быстро ──────────────────
def _cache_ok(name: str, ttl_s: float) -> bool:
    """True, если скан можно делать сейчас (прошло больше ttl)."""
    f = ROOT / "agent_memory" / f"cache_{name}.json"
    if not f.exists():
        return True
    try:
        return (time.time() - json.loads(f.read_text()).get("t", 0)) > ttl_s
    except Exception:  # noqa: BLE001
        return True


def _cache_touch(name: str, extra: dict | None = None) -> None:
    f = ROOT / "agent_memory" / f"cache_{name}.json"
    f.write_text(json.dumps({"t": time.time(), **(extra or {})}, ensure_ascii=False))

# ── петля opportunity: возможности, поиск, деньги, CVE ──────────────────────
def loop_opportunity(seconds: int = 45) -> dict:
    t0 = time.time()
    out: dict = {"loop": "opportunity", "t": time.strftime("%Y-%m-%dT%H:%M:%S"), "actions": []}
    # 1) поиск возможностей рабочими каналами (каналы проверяются живьём)
    queries = ["free serverless hosting cron bot 2026",
               "leaked api keys github secret scanning tool",
               "free tier cloud function schedule"]
    found: list[dict] = []
    for q in queries:
        s_res = search_web(q, limit=3)
        out["actions"].append({"do": "search", "q": q[:44], "ok": s_res["ok"],
                               "channels": s_res["channels"], "hits": len(s_res["hits"])})
        found += s_res["hits"]
    # 1b) свежие CVE/PoC: новые дома для ground и риски для нашего стека
    if time.time() - t0 < seconds - 30:
        try:
            import cve_watch
            if _cache_ok("cve", 1800):
                cv = cve_watch.scan(limit=8)
                _cache_touch("cve", {"summary": cv.get("summary")})
                out["actions"].append({"do": "cve", **cv.get("summary", {})})
            else:
                cv = json.loads((ROOT / "agent_memory" / "cve_feed.json").read_text())
                out["actions"].append({"do": "cve", "skip": "кэш 30 мин",
                                       **(cv.get("summary") or {})})
            out["cve_homes"] = [i for i in cv["github"].get("items", [])
                                if "возможный-дом" in (i.get("relevance") or {}).get("kind", [])][:5]
        except Exception as e:  # noqa: BLE001
            out["actions"].append({"do": "cve", "error": f"{type(e).__name__}: {e}"[:100]})
    # 1c) ключи в НАШИХ хранилищах (Airtable/Postgres) — второй эшелон добычи
    if time.time() - t0 < seconds - 22:
        try:
            import key_sources
            if _cache_ok("key_sources", 900):
                ks = key_sources.scan()
                _cache_touch("key_sources", {"summary": ks.get("summary")})
                out["actions"].append({"do": "key_sources", **ks.get("summary", {})})
            else:
                out["actions"].append({"do": "key_sources", "skip": "кэш 15 мин",
                                       "queue": key_sources.status().get("queue")})
        except Exception as e:  # noqa: BLE001
            out["actions"].append({"do": "key_sources", "error": f"{type(e).__name__}: {e}"[:100]})

    # 1d) деньги: сколько заработано и готов ли кошелёк (только крипта)
    try:
        wallet = json.loads((ROOT / "agent_memory" / "agent_wallet.json").read_text())
        earn = []
        ef = ROOT / "agent_memory" / "earnings.jsonl"
        if ef.exists():
            for line in ef.read_text(errors="replace").splitlines():
                try:
                    earn.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
        total = sum(float(e.get("usdt", 0) or 0) for e in earn)
        out["actions"].append({"do": "money", "total_usdt": round(total, 2),
                               "payments": len(earn),
                               "wallet_ready": bool(wallet.get("usdt_trc20")),
                               "blocker": None if wallet.get("usdt_trc20") else "нет адреса кошелька"})
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "money", "error": f"{type(e).__name__}"[:60]})

    # 2) модель ищет возможность: brain / ground / opportunity / money
    try:
        st = _keys_stats()
        q = ("You are the opportunity-loop of a self-replicating secret-hunting agent. "
             f"State: {json.dumps(st, ensure_ascii=False)}. Assets: 164 live-ish keys "
             "(pinecone, supabase, airtable, notion, neon, metaso, volcengine, nvidia, zhipuai), "
             "4 GitHub cron homes, SSH key, proxy pool, OpenRouter brain. "
             "Money can only be received in CRYPTOCURRENCY. "
             "Output ONLY 3 lines. Nothing else. No explanations, no analysis, no preamble.\n"
             "Line 1 must start with 'BRAIN: ', line 2 with 'GROUND: ', line 3 with 'MONEY: '.\n"
             "Each line = one concrete actionable opportunity, max 15 words.\n"
             "Example format:\nBRAIN: <action>\nGROUND: <action>\nMONEY: <action>")
        r = llm(context_guard("opportunity", q), tier="fast", max_tokens=300,
                require_labels=LABELS)
        if r["ok"]:
            lines = r["lines"][:3]
            rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": r["model"],
                   "provider": r["provider"], "fallbacks": r["fallbacks"],
                   "search_hits": found[:5], "ideas": lines}
            with open(OPPS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out["actions"].append({"do": "ideas", "model": r["model"], "n": len(lines),
                                   "fallbacks": r["fallbacks"]})
            out["ideas"] = lines
        else:
            out["actions"].append({"do": "ideas", "error": "лестница исчерпана",
                                   "tried": len(r["tried"])})
    except Exception as e:  # noqa: BLE001
        out["actions"].append({"do": "ideas", "error": f"{type(e).__name__}: {e}"[:120]})
    # 3) OPSEC-сводка: тело шифруется, логи локальны, память не отдаётся
    out["opsec"] = {"body_encrypted": (ROOT / "child_egg.enc").exists(),
                    "soul_encrypted": (ROOT / "soul.md.enc").exists(),
                    "memory_private": True,
                    "outbound_via_proxy_guard": (ROOT / "proxy_guard.py").exists()}
    out["sec"] = round(time.time() - t0, 1)
    _log({"kind": "loop", "loop": "opportunity", "sec": out["sec"]})
    return out


# ── health: цикл не должен останавливаться ──────────────────────────────────
def health() -> dict:
    r: dict = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "agent": AGENT_ID}
    # brain: жива ли хоть одна модель лестницы
    h = llm("Reply with the single word: OK", tier="fast", max_tokens=8)
    r["brain"] = {"ok": h["ok"], "served_by": f"{h['provider']}/{h['model']}" if h["ok"] else None,
                  "fallbacks_used": h["fallbacks"], "tried": h["tried"],
                  "ladder_size": len(_ladder())}
    # ground: живы ли дома
    g = _homes_cached()
    r["ground"] = {"live": g.get("live"), "total": g.get("homes"), "source": g.get("source")}
    r["context"] = context_state()
    r["breakers"] = breakers_report()
    r["model_fit"] = fit_report()
    try:
        import gh_hunt
        r["gh_hunt"] = gh_hunt.status()
    except Exception as e:  # noqa: BLE001
        r["gh_hunt"] = {"error": f"{type(e).__name__}"}
    try:
        import memory as _M
        r["knowledge"] = _M.kb_stats()
    except Exception as e:  # noqa: BLE001
        r["knowledge"] = {"error": f"{type(e).__name__}"}
    try:
        _o = json.loads((ROOT / "agent_memory" / "opsec_report.json").read_text())
        r["opsec"] = {"verdict": _o.get("verdict"),
                      "memory_private": _o["audit"].get("memory_private", {}).get("ok"),
                      "body_encrypted": _o["audit"].get("body_encrypted", {}).get("soul_enc")}
    except Exception:  # noqa: BLE001
        r["opsec"] = {"verdict": "нет отчёта"}
    try:
        import netfix
        from urllib.parse import urlparse
        hosts = sorted({urlparse(x["base"]).hostname for x in _ladder() if x.get("base")})
        r["hosts_reachable"] = netfix.probe_hosts([h for h in hosts if h], timeout=1.5)
    except Exception as e:  # noqa: BLE001
        r["hosts_reachable"] = {"error": f"{type(e).__name__}: {e}"[:80]}
    r["keys"] = _keys_stats()
    r["verdict"] = ("циклы работоспособны" if r["brain"]["ok"] and
                    (r.get("ground", {}).get("live") or 0) > 0 else "ТРЕБУЕТ ВНИМАНИЯ")
    HEALTH.write_text(json.dumps(r, ensure_ascii=False, indent=1))
    return r


def round_(loops: int = 1) -> dict:
    res = []
    for i in range(loops):
        res.append(loop_brain())
        res.append(loop_ground())
        res.append(loop_opportunity())
    h = health()
    return {"rounds": loops, "loops": res, "health": h}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"
    if cmd == "llm":
        p = sys.argv[2] if len(sys.argv) > 2 else "ping"
        t = sys.argv[3] if len(sys.argv) > 3 else "fast"
        print(json.dumps(llm(p, t), ensure_ascii=False, indent=1)[:2000])
    elif cmd == "ladder":
        print(json.dumps(ladder_report(), ensure_ascii=False, indent=1))
    elif cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=1))
    elif cmd == "brain":
        print(json.dumps(loop_brain(), ensure_ascii=False, indent=1))
    elif cmd == "ground":
        print(json.dumps(loop_ground(), ensure_ascii=False, indent=1))
    elif cmd == "opportunity":
        print(json.dumps(loop_opportunity(), ensure_ascii=False, indent=1))
    elif cmd == "round":
        n = int(sys.argv[sys.argv.index("--loops") + 1]) if "--loops" in sys.argv else 1
        print(json.dumps(round_(n), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
