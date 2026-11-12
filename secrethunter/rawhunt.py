#!/usr/bin/env python3
"""rawhunt.py — v2. Точечный поиск 'забытых' API-ключей видео-агрегаторов.
Качает конкретные файлы через raw.githubusercontent и грепает масками.
Строгий валидатор: значение не может перескакивать строку, не может быть
плейсхолдером/кодом, должно быть токеноподобным + проверка энтропии.
"""
import urllib.request, re, json, sys, math, os, socket
from concurrent.futures import ThreadPoolExecutor

TARGETS = json.load(open(sys.argv[1]))
OUT = sys.argv[2]
AGENT = os.environ.get("AGENT_ID") or ("sh-" + socket.gethostname()[:6])

# авто-маски из каталога models.dev (tools/checkers_generated.json): 205 env-имён
# чужих LLM-провайдеров — то, что обычные сканеры не знают.
def _auto_masks():
    import os
    here = os.path.dirname(os.path.abspath(globals().get("__file__", "tools/rawhunt.py")))
    fp = os.path.join(here, "checkers_generated.json")
    if not os.path.exists(fp):
        return []
    try:
        gen = json.load(open(fp))["checkers"]
    except Exception:
        return []
    out = []
    for pid, spec in gen.items():
        for e in spec.get("env", []):
            if e.endswith(("_API_KEY", "_KEY", "_TOKEN", "_PAT", "_SECRET")):
                out.append((pid.upper().replace("-", "_"), re.escape(e)))
    return out

# имя ключа -> маска; значение обязано быть на той же строке, без пробелов
NAME = r'(?i)\b(?P<n>%s)\b'
PATS = [
 ("KIE",        r'(?:KIE_API_KEY|KIE_API_TOKEN|KIE_KEY|KIE_AI_KEY|KIEAI_API_KEY|KIE_API_SECRET)'),
 ("APIMART",    r'(?:APIMART_API_KEY|API_APIMART_API_KEY|APIMART_KEY|APIMART_API_TOKEN)'),
 ("WAVESPEED",  r'(?:WAVESPEED_API_KEY|WAVESPEED_KEY|WAVESPEED_API_TOKEN)'),
 ("ATLASCLOUD", r'(?:ATLASCLOUD_API_KEY|ATLAS_API_KEY|ATLASCLOUD_KEY|ATLASCLOUD_TOKEN)'),
 ("AI302",      r'(?:AI302_API_KEY|API302_API_KEY|AI302_KEY|AI302_TOKEN)'),
 ("METASO",     r'(?:METASO_API_KEY|METASO_KEY|METASO_TOKEN|METASO_SECRET_KEY)'),
 ("COMPSHARE",  r'(?:COMPSHARE_API_KEY|COMPSHARE_KEY|COMPSHARE_SECRET_KEY|COMPSHARE_PUBLIC_KEY|COMPSHARE_PRIVATE_KEY)'),
 ("FAL",        r'(?:FAL_KEY|FAL_API_KEY)'),
 ("REPLICATE",  r'(?:REPLICATE_API_TOKEN|REPLICATE_API_KEY)'),
 ("OPENAI",     r'(?:OPENAI_API_KEY)'),
 ("ANTHROPIC",  r'(?:ANTHROPIC_API_KEY)'),
 ("GENERIC",    r'(?:[A-Z0-9_]*(?:API_?KEY|API_?TOKEN|ACCESS_?TOKEN|SECRET_?KEY)[A-Z0-9_]*)'),
]
# значение: только токен-символы, 12..120, без пробелов и кавычек внутри
VAL = r'(?P<v>[A-Za-z0-9_\-\.]{12,120})'
COMPILED = [(lab, re.compile(NAME % nm + r'["\']?[ \t]*[=:][ \t]*["\']?' + VAL)) for lab, nm in PATS]
COMPILED += [(lab, re.compile(NAME % nm + r'["\']?[ \t]*[=:][ \t]*["\']?' + VAL))
             for lab, nm in _auto_masks()]

# явные «не-ключи»: плейсхолдеры, имена переменных, URL-хосты, модели, код
BAD = re.compile(r'(?i)('
    r'your|xxx|here|example|placeholder|changeme|replace|dummy|fake|todo|none|null|'
    r'test[-_]?key|demo|sample|insert|enter|paste|abcdef|123456|_key$|_token$|'
    r'^https?$|^api$|^sk$|^key$|^bearer$|'
    r'^(?:minimax|wavespeed|atlas|kie|fal|apimart|metaso|compshare|openai|anthropic)[a-z0-9_\-/\.]*$|'
    r'^(?:os|sys|process|import|from|self|cls|env|getenv|environ)[\.\(]|'
    r'^[a-z_]+$)')
# известные префиксы реальных ключей
KNOWN_PREFIX = re.compile(r'^(sk-|sk_|r8_|hf_|eyJ|mk-|kie-|ws-|at-|ak-|pk-|xai-|gsk_|AIza|'
                          r'glpat-|ghp_|gho_|github_pat_|ntn_|nfp_|dop_v1_|ddp_|FlyV1|'
                          r'pat|key_|sbp_|pcsk_|sntrys_|pplx-|tvly-|exa-|fc-|csk-|sk-cp-|'
                          r'wsk_live_|shpat_|shpss_|sq0atp-|EAA|ya29\.|xox[baprs]-)')

# «беспрефиксные» форматы: UUID (sambanova/weaviate), base64-подобные, длинный hex
SHAPES = re.compile(
    r'^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'      # uuid
    r'|[0-9a-f]{32,64}'                                                       # hex
    r'|[A-Za-z0-9_\-]{28,}={0,2})$')                                         # base64/opaque


def looks_shaped(v: str) -> bool:
    """Ключ без известного префикса, но по форме похож на секрет."""
    return bool(SHAPES.match(v)) and entropy(v) >= 3.2

def entropy(s):
    if not s: return 0.0
    return -sum((s.count(c)/len(s))*math.log2(s.count(c)/len(s)) for c in set(s))

PLACEHOLDER = re.compile(r"change[-_ ]?me|your[-_ ]?|xxx+|placeholder|example|dummy|todo|"
                         r"django-insecure|test[-_]?key|secret[-_]?value|api[-_]?key|"
                         r"\bnull\b|\bnone\b|\btrue\b|\bfalse\b|^sk-different|^sk-abc|"
                         r"^sk-proj-cross|^AIzaSy-json|^env-|^owner|^fallback-|^foreign-",
                         re.I)


def looks_real(v):
    if len(v) < 12: return False
    # «плохие слова» (your|xxx|example|123456…) отсеиваем ТОЛЬКО у словоподобных
    # значений: внутри UUID/hex/base64 подстрока «123456» — норма, а не заглушка.
    if not looks_shaped(v) and BAD.search(v): return False
    if v.islower() and '_' in v and not KNOWN_PREFIX.match(v) and not looks_shaped(v):
        return False  # snake_case слово (но UUID/hex/base64 — пропускаем)
    e = entropy(v)
    if KNOWN_PREFIX.match(v):
        return e >= 3.2
    if looks_shaped(v):          # UUID / длинный hex / base64-строка
        return e >= 3.4 or (len(v) >= 32 and any(c.isdigit() for c in v))
    return e >= 3.5 and (any(c.isdigit() for c in v) or len(v) >= 24)

def fetch(t):
    """Скачать файл. Пробуем HEAD, затем main/master и несколько «вероятных» имён.

    Так «забытые» репозитории (где нет .env, но есть config.py или .env.example)
    всё равно попадают в корпус.
    """
    rp, path = t
    tries = [path]
    if path == ".env":
        tries += [".env.example", ".env.sample", ".env.template", ".env.local",
                  "env.example", ".env.dist", ".env.defaults"]
    for p in tries:
        for br in ("HEAD", "main", "master"):
            url = f"https://raw.githubusercontent.com/{rp}/{br}/{p}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "rawhunt/2.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return rp, p, r.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
    return rp, path, None

hits, stats = [], []
def dump():
    json.dump(hits, open(OUT, "w"), ensure_ascii=False, indent=1)
with ThreadPoolExecutor(max_workers=12) as ex:
    for rp, path, txt in ex.map(fetch, TARGETS):
        if txt is None:
            line = f"[--] {rp}/{path}: 404/err"
        else:
            n = 0
            for lab, pat in COMPILED:
                for mo in pat.finditer(txt):
                    v = mo.group('v')
                    if not looks_real(v): continue
                    if PLACEHOLDER.search(v): continue
                    if lab == "GENERIC" and not KNOWN_PREFIX.match(v) and not looks_shaped(v): continue
                    ctx = txt[max(0, mo.start()-25):mo.end()+3].replace("\n", " ").strip()
                    hits.append({"repo": rp, "file": path, "type": lab, "name": mo.group('n'),
                                 "value": v, "entropy": round(entropy(v), 2), "ctx": ctx[:240], "found_by": AGENT})
                    n += 1
            line = f"[ok] {rp}/{path}: {len(txt)}b, находок {n}"
        stats.append(line); print(line, flush=True); dump()

json.dump(hits, open(OUT, "w"), ensure_ascii=False, indent=1)
print("\n".join(stats))
print(f"\n=== НАХОДОК {len(hits)} ===")
seen = set()
for h in hits:
    k = (h["type"], h["value"])
    if k in seen: continue
    seen.add(k)
    print(f"{h['type']:11} H={h['entropy']:<4} {h['name']:24} = {h['value'][:58]:58} | {h['repo']} :: {h['file']}")
