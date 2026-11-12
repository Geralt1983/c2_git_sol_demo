#!/usr/bin/env python3
"""wallet.py — кошелёк агента: только криптовалюта, без зависимостей.

Задание создателя: «получать может только криптовалюту». Значит, агенту нужен свой
адрес — и он должен уметь его создать сам, в любой песочнице, где нет ни `eth_keys`,
ни `ecdsa`, ни `pynacl` (проверено: в нашей песочнице их нет).

Поэтому здесь всё с нуля на чистом Python:
  * keccak-f[1600] → keccak256 (для адреса Ethereum и контрольной суммы TRON);
  * secp256k1: скалярное умножение точки (для публичного ключа);
  * base58check (для TRON-адреса USDT TRC-20 — самая дешёвая сеть для приёма).

Самопроверка на известных векторах (это не «на глаз», а контрольные суммы):
  * keccak256("")    = c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
  * keccak256("abc") = 4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45
  * privkey=1        → 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf (EIP-55)

CLI:
  python3 wallet.py selftest      — проверить реализацию на векторах
  python3 wallet.py new           — создать кошелёк (ключ в agent_memory/wallet_secret.json)
  python3 wallet.py show          — показать адреса (без приватного ключа)
  python3 wallet.py check <txid>  — проверить поступление (публичный API, без ключей)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEM = ROOT / "agent_memory"
SECRET = MEM / "wallet_secret.json"      # приватный ключ: права 600, наружу не отдаём
PUBLIC = MEM / "agent_wallet.json"       # публичные адреса: можно показывать всем

# ── keccak-256 (чистая реализация, оригинальный padding 0x01) ────────────────
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_R = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
      [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M


def _keccak_f(a: list[int]) -> None:
    for rnd in range(24):
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x + 5 * y] ^= d[x]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _R[x][y])
        for x in range(5):
            for y in range(5):
                a[x + 5 * y] = b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y] & _M) & b[(x + 2) % 5 + 5 * y])
        a[0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    rate = 136                      # 1088 бит для keccak-256
    st = [0] * 25
    padded = bytearray(data) + b"\x01"
    while len(padded) % rate != 0:
        padded += b"\x00"
    padded[-1] ^= 0x80
    for off in range(0, len(padded), rate):
        blk = padded[off:off + rate]
        for i in range(rate // 8):
            st[i] ^= int.from_bytes(blk[i * 8:i * 8 + 8], "little")
        _keccak_f(st)
    out = b""
    for i in range(4):              # 32 байта = 4 полосы
        out += st[i].to_bytes(8, "little")
    return out


# ── secp256k1 ────────────────────────────────────────────────────────────────
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _inv(a: int, m: int = P) -> int:
    return pow(a, m - 2, m)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * _inv(2 * y1) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(k: int, pt=(GX, GY)):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def pubkey(priv: int) -> bytes:
    x, y = _mul(priv)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


# ── адреса ───────────────────────────────────────────────────────────────────
def eth_address(priv: int) -> str:
    raw = keccak256(pubkey(priv)[1:])[-20:].hex()
    # EIP-55: контрольная сумма по keccak адреса
    h = keccak256(raw.encode()).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(raw))


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58check(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    data = payload + chk
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    for b in data:
        if b == 0:
            out = "1" + out
        else:
            break
    return out


def tron_address(priv: int) -> str:
    """USDT TRC-20: base58check(0x41 + последние 20 байт keccak(pubkey))."""
    return b58check(b"\x41" + keccak256(pubkey(priv)[1:])[-20:])


def selftest() -> dict:
    out = {"vectors": [], "ok": True}
    v1 = keccak256(b"").hex()
    v2 = keccak256(b"abc").hex()
    out["vectors"].append({"keccak256('')": v1,
                           "match": v1 == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"})
    out["vectors"].append({"keccak256('abc')": v2,
                           "match": v2 == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"})
    addr = eth_address(1)
    out["vectors"].append({"privkey=1 eth": addr,
                           "match": addr == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"})
    out["ok"] = all(v["match"] for v in out["vectors"])
    return out


def new() -> dict:
    if SECRET.exists():
        return show()
    priv = int.from_bytes(os.urandom(32), "big") % N
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "curve": "secp256k1",
           "priv_hex": f"{priv:064x}", "eth": eth_address(priv), "usdt_trc20": tron_address(priv),
           "note": "приватный ключ — только здесь; наружу отдаём agent_wallet.json"}
    SECRET.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    try:
        os.chmod(SECRET, 0o600)
    except Exception:  # noqa: BLE001
        pass
    # публичные адреса — в agent_wallet.json (его можно показывать)
    pub = {}
    if PUBLIC.exists():
        try:
            pub = json.loads(PUBLIC.read_text())
        except Exception:  # noqa: BLE001
            pub = {}
    pub.update({"usdt_trc20": rec["usdt_trc20"], "usdt_erc20": rec["eth"],
                "created": rec["t"], "only_crypto": True})
    PUBLIC.write_text(json.dumps(pub, ensure_ascii=False, indent=1))
    return show()


def show() -> dict:
    if not SECRET.exists():
        return {"error": "кошелька нет — запусти wallet.py new"}
    r = json.loads(SECRET.read_text())
    return {"t": r["t"], "curve": r["curve"], "eth": r["eth"], "usdt_trc20": r["usdt_trc20"],
            "priv_hidden": r["priv_hex"][:6] + "…" + r["priv_hex"][-4:]}


def check(address: str = "") -> dict:
    """Проверить поступления по публичному API (ключи не нужны)."""
    if not address:
        if not SECRET.exists():
            return {"error": "нет кошелька"}
        address = json.loads(SECRET.read_text())["usdt_trc20"]
    url = f"https://api.trongrid.io/v1/accounts/{address}"
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "SecretsHunter/3.0"}), timeout=15) as r:
            j = json.loads(r.read())
        d = (j.get("data") or [{}])[0]
        return {"address": address, "http": 200, "usdt_balance": d.get("trc20", {}),
                "trx_balance": d.get("balance", 0)}
    except Exception as e:  # noqa: BLE001
        return {"address": address, "error": type(e).__name__}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "selftest":
        print(json.dumps(selftest(), ensure_ascii=False, indent=1))
    elif cmd == "new":
        print(json.dumps(new(), ensure_ascii=False, indent=1))
    elif cmd == "check":
        print(json.dumps(check(sys.argv[2] if len(sys.argv) > 2 else ""), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(show(), ensure_ascii=False, indent=1))
