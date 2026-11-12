#!/usr/bin/env python3
"""modelsdev.py — «разборка, что может каждый API» по каталогу models.dev.

models.dev/api.json — открытый каталог провайдеров и моделей (235 провайдеров,
6 МБ). Из него строим индекс возможностей: кто умеет text / image / video / audio /
pdf на вход и выход, tool_call, reasoning, attachment. Затем сопоставляем с нашими
найденными сервисами: «ключ от сервиса X → что этим ключом можно делать».

Запуск:
  python3 tools/modelsdev.py build     # скачать каталог и построить индекс
  python3 tools/modelsdev.py annotate  # сопоставить с ALL-FOUND-KEYS.json
  python3 tools/modelsdev.py caps video  # кто умеет видео
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "models_dev_index.json"
CAPS = ROOT / "api-capabilities.json"
KEYS = ROOT / "ALL-FOUND-KEYS.json"
SRC = "https://models.dev/api.json"
UA = "secrethunter/2"


def fetch() -> dict:
    req = urllib.request.Request(SRC, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def build() -> dict:
    d = fetch()
    prov, caps = {}, {}
    for pid, p in d.items():
        if not isinstance(p, dict):
            continue
        mods = p.get("models") or {}
        cset = set()
        for _, m in (mods.items() if isinstance(mods, dict) else []):
            if not isinstance(m, dict):
                continue
            mod = m.get("modalities") or {}
            for x in (mod.get("input") or []):
                cset.add("in:" + x)
            for x in (mod.get("output") or []):
                cset.add("out:" + x)
            for flag in ("tool_call", "reasoning", "attachment", "open_weights"):
                if m.get(flag):
                    cset.add(flag)
        prov[pid] = {"name": p.get("name"), "api": p.get("api"), "doc": p.get("doc"),
                     "env": p.get("env"), "npm": p.get("npm"),
                     "models": len(mods) if isinstance(mods, dict) else 0,
                     "caps": sorted(cset)}
        for c in cset:
            caps.setdefault(c, []).append(pid)
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": SRC,
           "providers": len(prov),
           "capability_index": {k: sorted(v) for k, v in
                                sorted(caps.items(), key=lambda x: -len(x[1]))},
           "provider_catalog": prov}
    INDEX.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"build: провайдеров {len(prov)}, индекс {INDEX.stat().st_size} байт")
    print("возможности:", {k: len(v) for k, v in sorted(caps.items(), key=lambda x: -len(x[1]))})
    return out


def annotate() -> dict:
    if not INDEX.exists():
        build()
    idx = json.loads(INDEX.read_text())
    cat = idx["provider_catalog"]
    recs = json.loads(KEYS.read_text()) if KEYS.exists() else []
    svcs = sorted({(r.get("service") or "").lower() for r in recs})
    known, unknown = {}, []
    for s in svcs:
        if s in cat:
            p = cat[s]
            known[s] = {"models": p["models"], "caps": p["caps"], "doc": p["doc"],
                        "env": p["env"], "api": p["api"]}
        else:
            unknown.append(s)
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "note": "что можно делать ключом от сервиса (по каталогу models.dev)",
           "services_total": len(svcs), "in_models_dev": len(known),
           "capabilities": known, "not_in_catalog": unknown}
    CAPS.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"annotate: {len(known)}/{len(svcs)} сервисов есть в каталоге → {CAPS.name}")
    for s, v in sorted(known.items(), key=lambda x: -x[1]["models"])[:20]:
        print(f"  {s:<14} моделей {v['models']:<5} {','.join(v['caps'][:6])}")
    return out


def caps(needle: str) -> None:
    idx = json.loads(INDEX.read_text())
    ci = idx["capability_index"]
    hit = [k for k in ci if needle.lower() in k.lower()]
    for k in hit:
        print(f"{k}: {len(ci[k])} провайдеров -> {', '.join(ci[k][:40])}")


def annotate_keys() -> None:
    """Прописать в ALL-FOUND-KEYS.json поле caps: что можно делать этим ключом."""
    idx = json.loads(INDEX.read_text())
    cat = idx["provider_catalog"]
    recs = json.loads(KEYS.read_text())
    n = 0
    for r in recs:
        s_ = (r.get("service") or "").lower()
        if s_ in cat:
            r["caps"] = cat[s_]["caps"]
            r["models_dev"] = cat[s_]["models"]
            n += 1
    KEYS.write_text(json.dumps(recs, ensure_ascii=False, indent=1))
    print(f"annotate_keys: проставлено caps у {n} записей")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "annotate"
    if cmd == "build":
        build()
    elif cmd == "annotate":
        annotate()
    elif cmd == "annotate-keys":
        annotate_keys()
    elif cmd == "caps":
        caps(sys.argv[2] if len(sys.argv) > 2 else "video")
    else:
        print(__doc__)
