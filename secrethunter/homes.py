#!/usr/bin/env python3
"""homes.py — контроль «домов»: реально ли копии агента ЖИВЫ.

Смысл: скопировать себя мало — надо знать, что копия работает. Дом без свежих
запусков = архив, а не живая копия. Скрипт проверяет по каждому дому:
репозиторий, наличие workflow, состояние workflow, последний запуск (когда, чем
вызван, чем закончился), сработал ли cron, свежесть артефактов памяти.

  python3 tools/homes.py check        # проверить все дома
  python3 tools/homes.py check --json # машинный вывод
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POOL = ROOT / "github_token_pool.json"
TARGETS = ROOT / "replication-targets.json"
OUT = ROOT / "homes-health.json"
UA = "secrethunter-homes/1.0"


def _get(url: str, key: str) -> tuple[int, dict | list]:
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            url, headers={"Authorization": f"Bearer {key}", "User-Agent": UA,
                          "Accept": "application/vnd.github+json"}), timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:  # noqa: BLE001
        return 0, {}


def _age(iso: str) -> float:
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def token(account: str) -> str:
    pool = json.loads(POOL.read_text())["tokens"]
    for t in pool:
        if t["account"] == account:
            return t["key"]
    return ""


def homes() -> list[dict]:
    d = json.loads(TARGETS.read_text())
    out = []
    for h in d.get("homes", []):
        repo = h.get("repo") or ""
        if not repo:
            continue
        out.append({"repo": repo, "path": h.get("path", ""),
                    "account": repo.split("/")[0]})
    return out


def check(verbose: bool = True) -> dict:
    res = []
    for h in homes():
        repo, acc = h["repo"], h["account"]
        k = token(acc)
        rec: dict = {"repo": repo, "account": acc, "has_token": bool(k)}
        if not k:
            rec["verdict"] = "нет токена"
            res.append(rec)
            continue
        s, info = _get(f"https://api.github.com/repos/{repo}", k)
        rec["repo_http"] = s
        if s != 200:
            rec["verdict"] = "репо недоступно"
            res.append(rec)
            continue
        rec["private"] = info.get("private")
        rec["pushed_at"] = info.get("pushed_at")
        s2, wf = _get(f"https://api.github.com/repos/{repo}/actions/workflows", k)
        wfs = wf.get("workflows", []) if isinstance(wf, dict) else []
        rec["workflows"] = [{"name": w["name"], "state": w["state"]} for w in wfs]
        s3, runs = _get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=10", k)
        rl = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
        rec["runs_total"] = runs.get("total_count") if isinstance(runs, dict) else None
        rec["events"] = sorted({r["event"] for r in rl})
        rec["cron_ever_ran"] = any(r["event"] == "schedule" for r in rl)
        if rl:
            last = rl[0]
            rec["last_run"] = {"at": last["created_at"], "age_h": _age(last["created_at"]),
                               "event": last["event"], "conclusion": last["conclusion"]}
        # вердикт: живой дом = есть workflow в state=active и запуск за последние 12 ч
        active = any(w["state"] == "active" for w in wfs)
        fresh = bool(rl) and 0 <= _age(rl[0]["created_at"]) <= 12
        if active and fresh:
            rec["verdict"] = "живой"
        elif active and rl:
            rec["verdict"] = "активен, но давно не запускался"
        elif active:
            rec["verdict"] = "workflow есть, запусков нет (архив)"
        else:
            rec["verdict"] = "нет активного workflow"
        if active and not rec["cron_ever_ran"]:
            rec["risk"] = "cron ни разу не сработал — дом держится только на ручном запуске"
        res.append(rec)
        if verbose:
            mark = "✅" if rec["verdict"] == "живой" else "⚠️"
            extra = f" | {rec.get('risk')}" if rec.get("risk") else ""
            lr = rec.get("last_run")
            when = f"{lr['age_h']}ч назад ({lr['event']}, {lr['conclusion']})" if lr else "нет"
            print(f"  {mark} {repo:<34} {rec['verdict']:<34} последний: {when}{extra}")
    OUT.write_text(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "homes": res},
                              ensure_ascii=False, indent=1))
    live = sum(1 for r in res if r.get("verdict") == "живой")
    print(f"\nhomes: живых {live}/{len(res)} — записано в {OUT.name}")
    return {"homes": len(res), "live": live}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        r = check(verbose="--json" not in sys.argv)
        if "--json" in sys.argv:
            print(json.dumps(r, ensure_ascii=False))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
