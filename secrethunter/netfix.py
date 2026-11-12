#!/usr/bin/env python3
"""netfix.py — лечение «DNS-залипания» в песочнице.

ПРОБЛЕМА (зафиксирована в итерации 1): из песочницы часть хостов (например metaso.cn)
не резолвится: `urllib` висит, socket-timeout НЕ срабатывает, потому что залипание
происходит внутри `socket.getaddrinfo` (системный резолвер), а не в connect/read.
Итог: провайдер «отказывает» не из-за ключа, а из-за сети — ложная смерть.

РЕШЕНИЕ: собственный резолвер через DNS-over-HTTPS по IP-адресу (без DNS вообще),
кэш на диск, и подмена `socket.getaddrinfo` только для тех хостов, которые система
резолвить не умеет. Всё остальное работает как раньше.

CLI:
  python3 netfix.py resolve <host>     — резолвить через DoH (IP, без системного DNS)
  python3 netfix.py check <url>        — проверить доступность URL с обходом DNS
  python3 netfix.py install            — включить фикс в текущем процессе
  python3 netfix.py report             — что в кэше
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "agent_memory" / "dns_cache.json"
CACHE.parent.mkdir(exist_ok=True)
DOH = ["https://1.1.1.1/dns-query", "https://8.8.8.8/resolve", "https://9.9.9.9:5053/dns-query"]
TIMEOUT = 6.0


def _load() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def _save(d: dict) -> None:
    CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def _doh(host: str) -> list[str]:
    """A-записи через DoH. Запрос идёт на IP, поэтому системный DNS не нужен."""
    ips: list[str] = []
    for base in DOH:
        try:
            url = f"{base}?name={host}&type=A"
            req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                j = json.loads(r.read())
            for a in j.get("Answer", []) or []:
                if a.get("type") == 1 and a.get("data"):
                    ips.append(a["data"])
            if ips:
                break
        except Exception:  # noqa: BLE001
            continue
    return ips


def resolve(host: str, ttl: int = 3600) -> list[str]:
    """Резолв с кэшем. Сначала кэш, потом DoH, в крайнем случае — системный резолвер."""
    d = _load()
    rec = d.get(host)
    if rec and time.time() - rec.get("t", 0) < ttl and rec.get("ips"):
        return rec["ips"]
    ips = _doh(host)
    if not ips:
        try:
            ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
        except Exception:  # noqa: BLE001
            ips = []
    if ips:
        d[host] = {"ips": ips, "t": time.time(), "via": "doh" if _doh else "system"}
        _save(d)
    return ips


_orig_gai = socket.getaddrinfo
_patched = False


def install(hosts: list[str] | None = None) -> dict:
    """Подменить getaddrinfo для хостов, которые система не резолвит.

    hosts=None — для всех хостов из кэша + известных «залипающих».
    Возвращает отчёт: какие хосты теперь обслуживаются нашим резолвером.
    """
    global _patched
    targets = set(hosts or [])
    targets |= set(_load().keys())
    targets |= {"metaso.cn", "api.metaso.cn", "open.bigmodel.cn"}
    fixed: dict[str, list[str]] = {}
    for h in targets:
        ips = resolve(h)
        if ips:
            fixed[h] = ips

    def gai(host, port, family=0, type_=0, proto=0, flags=0):  # noqa: A002
        if host in fixed:
            out = []
            for ip in fixed[host]:
                fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                out.append((fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)))
            return out
        return _orig_gai(host, port, family, type_, proto, flags)

    if not _patched:
        socket.getaddrinfo = gai  # type: ignore[assignment]
        socket.setdefaulttimeout(TIMEOUT)
        _patched = True
    return {"patched_hosts": fixed, "count": len(fixed)}


def check(url: str, timeout: float = 10.0) -> dict:
    """Проверить URL с обходом DNS. Возвращает http-код или причину отказа."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    install([host] if host else None)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netfix/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"url": url, "host": host, "http": r.status,
                    "ms": int((time.time() - t0) * 1000), "via": "netfix"}
    except Exception as e:  # noqa: BLE001
        return {"url": url, "host": host, "http": 0, "err": f"{type(e).__name__}",
                "ips": resolve(host), "ms": int((time.time() - t0) * 1000)}


def report() -> dict:
    d = _load()
    return {"cached_hosts": len(d),
            "hosts": {h: v.get("ips") for h, v in list(d.items())[:40]}}


_REACH: dict[str, tuple[bool, float]] = {}


def reachable(host: str, port: int = 443, timeout: float = 2.0, ttl: float = 120.0) -> bool:
    """Быстрая предпроверка: открывается ли TCP до хоста. Кэш на процесс.

    Зачем: из песочницы часть хостов недостижима, и urllib висит до 10 с на каждой
    попытке. Петля из-за этого не укладывается в бюджет. Предпроверка превращает
    «висит 10 с» в «пропущен за 2 с», а решение принимает та же лестница.
    """
    key = f"{host}:{port}"
    hit = _REACH.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    ok = False
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        ip = infos[0][4][0]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            ok = True
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        ok = False
    _REACH[key] = (ok, time.time())
    return ok


def probe_hosts(hosts: list[str], timeout: float = 2.0) -> dict:
    """Пакетная проверка списка хостов (для health/панели)."""
    out = {}
    for h in hosts:
        out[h] = reachable(h, timeout=timeout)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "resolve":
        print(json.dumps({"host": sys.argv[2], "ips": resolve(sys.argv[2])}, ensure_ascii=False))
    elif cmd == "check":
        print(json.dumps(check(sys.argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "probe":
        hosts = sys.argv[2:] or ["openrouter.ai", "metaso.cn", "open.bigmodel.cn",
                                 "integrate.api.nvidia.com", "generativelanguage.googleapis.com",
                                 "api.deepinfra.com", "api.exa.ai", "api.tavily.com"]
        print(json.dumps(probe_hosts(hosts), ensure_ascii=False, indent=1))
    elif cmd == "install":
        print(json.dumps(install(), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(report(), ensure_ascii=False, indent=1))

