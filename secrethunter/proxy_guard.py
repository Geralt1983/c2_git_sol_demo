#!/usr/bin/env python3
"""proxy_guard.py — «глаза» агента: сетевой слой, который НЕ светит реальный IP.

Задачи:
  1. Держать пул живых прокси (источник — secrethog-proxy-finder).
  2. Проверять каждый прокси ДВУМЯ тестами: (а) подмена IP видна на ipify,
     (б) целевой хост (grep.app / github) реально отвечает 200.
  3. Отсекать ЛОЖНЫЕ ответы: HTML-страница 429/403 вместо JSON, пустой ответ,
     строки-ошибки ('expected string or bytes-like object'), совпадение IP с нашим.
  4. Отдавать fetch() с ротацией прокси, ротацией UA и честным флагом
     `via` = proxy|direct, чтобы в отчёте было видно, где мы засветились.

Философия: прокси пробуем ПЕРВЫМ, direct — только как аварийный фолбэк,
и каждый такой случай пишется в лог (память агента).
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POOL_CACHE = ROOT / "proxyfinder" / "secrethog-proxy-finder" / "data" / "proxy_pool_cache.json"
STATE = ROOT / "agent_proxy_state.json"
LOG = ROOT / "agent_memory" / "proxy.log"

UAS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]

# Признаки «ложного» ответа: нас не пустили, а не «данных нет».
FALSE_MARKERS = (
    b"expected string or bytes-like object",
    b"<!DOCTYPE html>",
    b"<html",
    b"Rate limit",
    b"Too Many Requests",
)


def _log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")


def _opener(proxy: str | None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _raw_get(url: str, proxy: str | None, timeout: float, ua: str | None = None,
             headers: dict | None = None):
    hdr = {
        "User-Agent": ua or random.choice(UAS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # ВАЖНО: заголовки авторизации (Api-Key, Authorization, x-api-key…) обязаны
    # доехать до сервиса. Без них любой живой ключ получает 401 — это была
    # регрессия итерации 3: прокси-пул «убивал» живые ключи.
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(
        url,
        headers=hdr,
    )
    t = time.time()
    try:
        r = _opener(proxy).open(req, timeout=timeout)
        return r.status, r.read(), round(time.time() - t, 2), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(), round(time.time() - t, 2), None
    except Exception as e:  # noqa: BLE001
        return 0, b"", round(time.time() - t, 2), type(e).__name__


def my_ip() -> str:
    try:
        _, b, _, _ = _raw_get("https://api.ipify.org?format=json", None, 10)
        return json.loads(b)["ip"]
    except Exception:  # noqa: BLE001
        return "unknown"


def load_pool() -> list[str]:
    if not POOL_CACHE.exists():
        return []
    try:
        d = json.loads(POOL_CACHE.read_text())
        return [p["url"] for p in d.get("proxies", [])]
    except Exception:  # noqa: BLE001
        return []


def looks_false(body: bytes) -> bool:
    """Ответ похож на заглушку/бан, а не на данные."""
    if not body:
        return True
    head = body[:400]
    for m in FALSE_MARKERS:
        if m in head:
            return True
    return False


def validate(proxy: str, target: str = "https://grep.app/api/search?q=KIE_API_KEY",
             real_ip: str | None = None) -> dict:
    """Живой ли прокси: (1) подменяет IP, (2) достаёт целевой хост, (3) не врёт."""
    out = {"proxy": proxy, "ip": None, "ip_ok": False, "target_status": 0,
           "target_ok": False, "latency": None, "false_response": False}
    s, b, dt, err = _raw_get("https://api.ipify.org?format=json", proxy, 6)
    if s == 200:
        try:
            ip = json.loads(b)["ip"]
            out["ip"] = ip
            out["ip_ok"] = bool(ip) and ip != real_ip
        except Exception:  # noqa: BLE001
            pass
    if not out["ip_ok"]:
        return out
    s2, b2, dt2, _ = _raw_get(target, proxy, 8)
    out["target_status"] = s2
    out["latency"] = dt2
    out["false_response"] = looks_false(b2) or (s2 == 200 and not b2.lstrip().startswith(b"{"))
    out["target_ok"] = s2 == 200 and not out["false_response"]
    return out


def refresh(target: str | None = None, limit: int = 40, workers: int = 12) -> list[str]:
    """Перепроверить пул ПАРАЛЛЕЛЬНО (песочница режет по 60с на команду)."""
    from concurrent.futures import ThreadPoolExecutor
    real = my_ip()
    tgt = target or "https://grep.app/api/search?q=KIE_API_KEY"
    cand = load_pool()[:limit]
    good = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(lambda p: validate(p, tgt, real), cand):
            _log(f"validate {r['proxy']} ip={r['ip']} ip_ok={r['ip_ok']} "
                 f"target={r['target_status']} false={r['false_response']}")
            if r["ip_ok"] and (target is None or r["target_ok"]):
                good.append(r["proxy"])
    STATE.write_text(json.dumps({"ts": time.time(), "real_ip": real, "good": good,
                                 "checked": len(cand)}, indent=1))
    return good


class Guard:
    """Ротация прокси + честный учёт, где мы засветили реальный IP."""

    def __init__(self, good: list[str] | None = None):
        self.real_ip = my_ip()
        if good is None:
            st = json.loads(STATE.read_text()) if STATE.exists() else {}
            good = st.get("good") or []
        self.good = good
        self.i = 0
        self.stats = {"proxy": 0, "direct": 0, "fail": 0}
        # хост -> ts, до которого ходим только напрямую (прокси его не тянет)
        self.host_direct: dict[str, float] = {}

    def next_proxy(self) -> str | None:
        if not self.good:
            return None
        p = self.good[self.i % len(self.good)]
        self.i += 1
        return p

    def get(self, url: str, timeout: float = 15, allow_direct: bool = True,
            headers: dict | None = None):
        """Вернуть (status, body, meta). Сначала прокси, потом (по нужде) direct.

        ВАЖНО: у бесплатных прокси разная специализация — один тянет grep.app и не
        тянет raw.githubusercontent. Поэтому сначала пробуем прокси с КОРОТКИМ
        таймаутом (быстрый отсев «мёртвых для этого хоста»), и только потом direct.
        """
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        skip_proxy = self.host_direct.get(host, 0) > time.time()
        probe_t = min(timeout, 5.0)
        for _ in range(0 if skip_proxy else min(3, max(1, len(self.good)))):
            p = self.next_proxy()
            if not p:
                break
            s, b, dt, err = _raw_get(url, p, probe_t, headers=headers)
            if s == 200 and not looks_false(b):
                self.stats["proxy"] += 1
                return s, b, {"via": "proxy", "proxy": p, "latency": dt}
            _log(f"proxy-miss {p} {url[:80]} status={s} false={looks_false(b)} err={err}")
        if not allow_direct:
            self.stats["fail"] += 1
            return 0, b"", {"via": "none"}
        if self.good and not skip_proxy:
            self.host_direct[host] = time.time() + 600   # 10 мин: этот хост — напрямую
        s, b, dt, err = _raw_get(url, None, timeout, headers=headers)
        self.stats["direct"] += 1
        _log(f"DIRECT (real IP exposed) {url[:90]} status={s}")
        return s, b, {"via": "direct", "latency": dt, "err": err}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        g = refresh()
        print(f"real_ip={my_ip()} good={len(g)}")
        for x in g:
            print(" ", x)
    else:
        g = Guard()
        print("real_ip:", g.real_ip, "pool:", len(g.good))
        s, b, m = g.get("https://grep.app/api/search?q=KIE_API_KEY")
        print(s, m, b[:120])
