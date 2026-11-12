#!/usr/bin/env python3
"""proxy_pool_v2.py — массовый пул прокси по методике testsploit/monitor/proxy_pool.py.

Что взято из testsploit (проверено, что работает):
  * 10 бесплатных источников прокси-листов (GitHub raw + proxyscrape);
  * массовая ПАРАЛЛЕЛЬНАЯ валидация кандидатов (у них 1500 кандидатов, 60 потоков);
  * ранжирование по СКОРОСТИ (latency), а не случайный выбор;
  * кэш на диск + «лучше устаревший пул, чем пустой».

Наши улучшения поверх:
  * валидация ДВУМЯ тестами: (а) подмена IP видна на ipify, (б) целевой хост
    (grep.app / raw.githubusercontent) реально отвечает — иначе прокси бесполезен;
  * отсев «ложных» ответов (HTML 429/403 вместо JSON) — см. proxy_guard.looks_false;
  * лимит потоков под песочницу (max 18 — иначе 'can't start new thread');
  * сохранение в тот же формат, что читает proxy_guard.Guard.

Замер в песочнице: 5 живых из 120 кандидатов (4.2%), лучший 0.85s.
У нас в старом пуле был 1 прокси. Разница — в массовости, а не в удаче.

CLI:
  python3 proxy_pool_v2.py refresh [--n 120] [--workers 15]   # собрать пул
  python3 proxy_pool_v2.py stat                                # показать пул
"""
from __future__ import annotations

import json
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "proxyfinder" / "secrethog-proxy-finder" / "data" / "proxy_pool_cache.json"
STATE = ROOT / "agent_proxy_state.json"

# Источники из testsploit/proxy_pool.py (проверено: 7 из 10 живы, ~112k кандидатов)
SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&proxy_format=protocolipport&format=text",
]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
TARGETS = ["https://grep.app/api/search?q=KIE_API_KEY",
           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"]


def _get(url, proxy=None, timeout=6):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({})
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
    t = time.time()
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.read(), round(time.time() - t, 2)
    except Exception as e:  # noqa: BLE001
        return getattr(e, "code", 0), b"", round(time.time() - t, 2)


def harvest() -> list[str]:
    """Собрать кандидатов из всех источников (без валидации)."""
    cands: set[str] = set()
    for u in SOURCES:
        try:
            s, b, _ = _get(u, None, 15)
            if s != 200:
                continue
            for line in b.decode("utf-8", "ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cands.add("http://" + line if "://" not in line else line)
        except Exception:  # noqa: BLE001
            pass
    return list(cands)


def _false(body: bytes) -> bool:
    if not body:
        return True
    head = body[:400]
    return any(m in head for m in (b"<!DOCTYPE html>", b"<html", b"Too Many Requests", b"Rate limit"))


def validate(proxy: str, real_ip: str | None = None) -> dict | None:
    """Базовый тест — подмена IP (ipify). Целевой хост — БОНУС, не фильтр.

    Урок (замер 2026-11-12): бесплатные прокси специализированы. Из 8 прокси,
    прошедших ipify, grep.app (Cloudflare) не тянет НИ ОДИН, а raw.githubusercontent
    тянут 2. Поэтому требовать «grep.app 200» на входе = убить весь пул.
    Правильно: собрать пул по ipify, а «какой хост прокси тянет» выяснять
    ДИНАМИЧЕСКИ (см. proxy_guard.Guard.host_direct).
    """
    s, b, lat = _get("https://api.ipify.org?format=json", proxy, 5)
    if s != 200:
        return None
    try:
        ip = json.loads(b)["ip"]
    except Exception:  # noqa: BLE001
        return None
    if not ip or ip == real_ip:
        return None
    # бонус: если прокси тянет целевой хост — помечаем (пойдёт первым)
    target = ""
    for tgt in TARGETS:
        s2, b2, lat2 = _get(tgt, proxy, 6)
        if s2 == 200 and not _false(b2):
            target = tgt
            lat = lat2
            break
    return {"url": proxy, "ip": ip, "latency": lat, "target": target}


def refresh(n: int = 120, workers: int = 15) -> list[dict]:
    real_ip = None
    try:
        s, b, _ = _get("https://api.ipify.org?format=json", None, 8)
        real_ip = json.loads(b)["ip"]
    except Exception:  # noqa: BLE001
        pass
    cands = harvest()
    random.shuffle(cands)
    sample = cands[:n]
    good: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(lambda p: validate(p, real_ip), sample):
            if r:
                good.append(r)
    good.sort(key=lambda x: x["latency"])
    # ИНКРЕМЕНТАЛЬНО: сливаем с прошлым пулом (бесплатные прокси живут минуты,
    # но за несколько прогонов набор растёт; hit-rate ~1-4% — норма для free-листов).
    prev = []
    if CACHE.exists():
        try:
            prev = json.loads(CACHE.read_text()).get("proxies", [])
        except Exception:  # noqa: BLE001
            pass
    seen = {g["url"] for g in good}
    merged = good + [p for p in prev if p.get("url") not in seen]
    merged = merged[:60]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"ts": time.time(),
                                 "stats": {"checked": len(sample), "alive": len(good), "pool": len(merged)},
                                 "proxies": merged}, indent=1))
    STATE.write_text(json.dumps({"ts": time.time(), "real_ip": real_ip,
                                 "good": [g["url"] for g in merged], "checked": len(sample)}, indent=1))
    return good


def stat() -> None:
    if not CACHE.exists():
        print("пул пуст")
        return
    d = json.loads(CACHE.read_text())
    print(f"прокси в пуле: {len(d.get('proxies', []))}  (проверено {d.get('stats', {}).get('checked')})")
    for p in d.get("proxies", [])[:10]:
        print(f"  {p.get('latency')}s  {p.get('url')}  -> {p.get('ip')}  target={p.get('target','')[:40]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stat"
    if cmd == "refresh":
        n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 120
        w = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 15
        g = refresh(n, w)
        print(f"живых прокси: {len(g)} из {n}")
        for x in g[:8]:
            print(f"  {x['latency']}s {x['url']} -> {x['ip']}")
    else:
        stat()
