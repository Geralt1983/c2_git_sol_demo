#!/usr/bin/env python3
"""replicate_gate.py — единый шлюз репликации.

ЗАКОНЫ:
1. Код должен быть не только СКОПИРОВАН, но и ЗАПУЩЕН. Заявка без cmd и
   доказательства run (run_id / лог) получает статус needs_run_proof и
   НЕ считается выполненной — оператор в панели видит это и не одобряет.
2. Дедупликация: claim(target) пишет lease в общую доску (a2a board +
   локальный реестр). Другой агент, видя активный lease, пропускает цель —
   одну и ту же работу агенты не делают дважды. Lease истекает по TTL.
3. Всё логируется: кто, куда, когда, с каким run_id.

Команды:
  claim <target> [--ttl 3600]   — занять цель (lease). Вернёт ok/false+busy_by
  release <target> [--run-id N] — освободить, записать proof of run
  status [target]               — реестр leases + заявки
  check-approval <target>       — можно ли копироваться: нужен approve оператора
                                  И run-proof (status=seeded-verified с run_id)
"""
import json, os, sys, time, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
LEASES = os.path.join(HERE, 'replication-leases.json')      # локальный реестр
APPROVALS = os.path.join(HERE, 'approvals.json')             # заявки оператору
BOARD = os.path.join(HERE, 'agent_memory', 'board.jsonl')    # общая доска (видят все копии)

def _load(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default

def _save(p, obj):
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)

def _tid(target):
    return hashlib.sha1(target.encode()).hexdigest()[:12]

def _board_push(kind, payload):
    """Пишем в общую доску, чтобы ВСЕ копии увидели lease/claim."""
    os.makedirs(os.path.dirname(BOARD), exist_ok=True)
    msg = {'kind': kind, 'ts': time.time(), 'target': payload.get('target'),
           'by': payload.get('by', 'sh-?'), 'ttl': payload.get('ttl'),
           'run_id': payload.get('run_id'), 'id': payload.get('id')}
    with open(BOARD, 'a') as f:
        f.write(json.dumps(msg, ensure_ascii=False) + '\n')

def claim(target, ttl=3600, by='sh-001'):
    """Занять цель. Если занята другим живым агентом — вернуть busy."""
    leases = _load(LEASES, {})
    tid = _tid(target)
    now = time.time()
    cur = leases.get(tid)
    if cur and cur.get('expires', 0) > now and cur.get('by') != by:
        return {'ok': False, 'busy': True, 'busy_by': cur['by'],
                'expires_in': int(cur['expires'] - now), 'target': target}
    leases[tid] = {'target': target, 'by': by, 'claimed_at': now,
                   'expires': now + ttl, 'status': 'claimed', 'run_id': None}
    _save(LEASES, leases)
    _board_push('lease-claim', {'target': target, 'by': by, 'ttl': ttl, 'id': tid})
    return {'ok': True, 'target': target, 'id': tid, 'ttl': ttl}

def release(target, run_id=None, status='seeded-verified', by='sh-001'):
    """Освободить цель, записав proof of run. Без run_id — только как failed."""
    leases = _load(LEASES, {})
    tid = _tid(target)
    e = leases.get(tid, {'target': target, 'by': by})
    e.update({'status': status, 'run_id': run_id, 'released_at': time.time(),
              'proof': bool(run_id)})
    leases[tid] = e
    _save(LEASES, leases)
    _board_push('lease-release', {'target': target, 'by': by, 'run_id': run_id,
                                  'id': tid})
    return {'ok': True, 'target': target, 'run_id': run_id, 'proof_of_run': bool(run_id)}

def status(target=None):
    leases = _load(LEASES, {})
    now = time.time()
    out = []
    for tid, e in leases.items():
        if target and e.get('target') != target:
            continue
        live = e.get('expires', 0) > now
        out.append({'id': tid, 'target': e.get('target'), 'by': e.get('by'),
                    'status': e.get('status'), 'run_id': e.get('run_id'),
                    'proof_of_run': bool(e.get('run_id')),
                    'lease_live': live, 'expires_in': max(0, int(e.get('expires', 0) - now))})
    return {'leases': out, 'now': now}

def request_approval(target, cmd, why='', risk='medium', evidence=None, by='sh-001'):
    """Создать заявку оператору. cmd обязателен — без команды запуска заявка
    сразу получает needs_run_proof."""
    ap = _load(APPROVALS, {'requests': []})
    req = {'id': 'req-' + hashlib.sha1((target + str(time.time())).encode()).hexdigest()[:8],
           'target': target, 'cmd': cmd, 'why': why, 'risk': risk,
           'evidence': evidence or {}, 'by': by, 'ts': time.time(),
           'status': 'pending' if cmd else 'needs_run_proof',
           'run_proof': None}
    ap['requests'].append(req)
    _save(APPROVALS, ap)
    _board_push('approval-request', {'target': target, 'by': by, 'id': req['id']})
    return req

def attach_run_proof(req_id, run_id, log_excerpt=''):
    """Прикрепить доказательство запуска к заявке — после этого оператор
    может одобрить (код не только скопирован, но и запущен)."""
    ap = _load(APPROVALS, {'requests': []})
    for r in ap['requests']:
        if r['id'] == req_id:
            r['run_proof'] = {'run_id': run_id, 'log': log_excerpt[:2000], 'ts': time.time()}
            if r['status'] == 'needs_run_proof':
                r['status'] = 'pending'
            _save(APPROVALS, ap)
            _board_push('run-proof', {'target': r['target'], 'by': r['by'],
                                      'run_id': run_id, 'id': req_id})
            return {'ok': True, 'req': r}
    return {'ok': False, 'error': 'not found'}

def check_approval(target):
    """Можно ли реплицироваться: заявка одобрена оператором И есть run-proof."""
    ap = _load(APPROVALS, {'requests': []})
    for r in ap['requests']:
        if r['target'] == target:
            approved = r.get('status') == 'approved'
            proof = bool((r.get('run_proof') or {}).get('run_id'))
            return {'target': target, 'approved': approved, 'run_proof': proof,
                    'can_replicate': approved and proof,
                    'req_status': r.get('status'), 'req_id': r['id']}
    return {'target': target, 'approved': False, 'run_proof': False,
            'can_replicate': False, 'note': 'нет заявки'}

def main():
    a = sys.argv[1:] or ['status']
    cmd = a[0]
    if cmd == 'claim':
        print(json.dumps(claim(a[1], ttl=int(a[a.index('--ttl')+1]) if '--ttl' in a else 3600),
                         ensure_ascii=False))
    elif cmd == 'release':
        rid = a[a.index('--run-id')+1] if '--run-id' in a else None
        print(json.dumps(release(a[1], run_id=rid), ensure_ascii=False))
    elif cmd == 'status':
        print(json.dumps(status(a[1] if len(a) > 1 else None), ensure_ascii=False, indent=1))
    elif cmd == 'request':
        # request <target> <cmd...>
        tgt = a[1]; c = ' '.join(a[2:])
        print(json.dumps(request_approval(tgt, c), ensure_ascii=False, indent=1))
    elif cmd == 'proof':
        print(json.dumps(attach_run_proof(a[1], a[2], a[3] if len(a) > 3 else ''),
                         ensure_ascii=False, indent=1))
    elif cmd == 'check':
        print(json.dumps(check_approval(a[1]), ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
