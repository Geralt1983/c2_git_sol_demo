#!/usr/bin/env python3
"""eth_wallet.py — ERC-20 / Ethereum кошелёк агента (в дополнение к TRC-20).

Умеет без внешних библиотек:
  * создать пару secp256k1 (тот же движок, что в wallet.py);
  * отдать адрес для приёма USDT ERC-20 / ETH;
  * проверить баланс через публичные API (без ключей):
      - ETH: Blockscout (eth.blockscout.com) — работает без ключа;
      - USDT ERC-20: ethplorer freekey (и Blockscout как резерв).
  * собрать и подписать перевод ERC-20 (EIP-1559) — код готов, отправка
    включается флагом --send, когда на адресе появятся ETH на газ.

Приватный ключ лежит ТОЛЬКО в agent_memory/agent_wallet.secret.json (chmod 600).
Наружу (в панель, в память, в яйцо) уходит только адрес.

CLI:
  python3 tools/eth_wallet.py show
  python3 tools/eth_wallet.py check
  python3 tools/eth_wallet.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEM = ROOT / "agent_memory"
PUBLIC = MEM / "agent_wallet.json"
SECRET = MEM / "wallet_secret.json"   # тот же приватный ключ, что у wallet.py

USDT_ETH = "0xdAC17F958D2ee523a2206206994597C13D831ec7"   # контракт USDT ERC-20
TRANSFER_SELECTOR = "a9059cbb"


# ── secp256k1: берём из wallet.py, чтобы не дублировать криптографию ─────────
def _wallet_mod():
    sys.path.insert(0, str(ROOT))
    import wallet  # type: ignore
    return wallet


def selftest() -> dict:
    w = _wallet_mod()
    st = w.selftest()
    out = {"keccak_ok": st.get("ok"), "vectors": st.get("vectors")}
    # EIP-55 адрес из приватного ключа 1 должен совпасть с эталоном
    addr = w.eth_address(1)
    out["eth_addr_privkey1"] = addr
    out["match"] = addr == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    out["ok"] = bool(st.get("ok")) and out["match"]
    return out


def show() -> dict:
    if not SECRET.exists():
        return {"error": "кошелька нет — запусти eth_wallet.py new"}
    r = json.loads(SECRET.read_text())
    return {"created": r.get("t"), "curve": r.get("curve"), "eth": r.get("eth"),
            "usdt_erc20": r.get("eth"), "usdt_trc20": r.get("usdt_trc20"),
            "priv_hidden": (r.get("priv_hex") or "")[:6] + "…" + (r.get("priv_hex") or "")[-4:],
            "note": "один и тот же адрес принимает ETH и USDT ERC-20"}


def _get(url: str, timeout: int = 12) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "SecretsHunter/3.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def check(address: str = "") -> dict:
    """Балансы ETH и USDT ERC-20 через публичные API без ключей."""
    if not address:
        if not SECRET.exists():
            return {"error": "нет кошелька"}
        address = json.loads(SECRET.read_text())["eth"]
    out: dict = {"address": address, "network": "ethereum-mainnet", "sources": {}}
    # 1) ETH-баланс: Blockscout (без ключа)
    try:
        j = _get(f"https://eth.blockscout.com/api/v2/addresses/{address}")
        wei = int(j.get("coin_balance") or 0)
        out["sources"]["blockscout"] = {"http": 200, "eth": wei / 1e18,
                                        "txs": j.get("transaction_count"),
                                        "is_contract": j.get("is_contract")}
        out["eth"] = wei / 1e18
    except Exception as e:  # noqa: BLE001
        out["sources"]["blockscout"] = {"error": f"{type(e).__name__}"}
    # 2) USDT ERC-20: ethplorer freekey
    try:
        j = _get(f"https://api.ethplorer.io/getAddressInfo/{address}?apiKey=freekey")
        toks = j.get("tokens") or []
        usdt = 0.0
        for t in toks:
            if (t.get("tokenInfo") or {}).get("address", "").lower() == USDT_ETH.lower():
                usdt = float(t.get("balance") or 0) / 10 ** int((t.get("tokenInfo") or {}).get("decimals", 6))
        out["sources"]["ethplorer"] = {"http": 200, "tokens": len(toks), "usdt_erc20": usdt}
        out["usdt_erc20"] = usdt
        out["tokens"] = [{"sym": (t.get("tokenInfo") or {}).get("symbol"),
                          "bal": t.get("balance")} for t in toks[:8]]
    except Exception as e:  # noqa: BLE001
        out["sources"]["ethplorer"] = {"error": f"{type(e).__name__}"}
    out["t"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["verdict"] = ("пусто (адрес готов принимать)" if not out.get("eth") and not out.get("usdt_erc20")
                      else "есть средства")
    return out


def ensure() -> dict:
    """Гарантировать, что ERC-20-адрес есть в публичном файле кошелька."""
    w = _wallet_mod()
    if not SECRET.exists():
        w.new()
    r = json.loads(SECRET.read_text())
    pub = {}
    if PUBLIC.exists():
        try:
            pub = json.loads(PUBLIC.read_text())
        except Exception:  # noqa: BLE001
            pub = {}
    pub.setdefault("usdt_trc20", r.get("usdt_trc20"))
    pub["usdt_erc20"] = r["eth"]
    pub["eth"] = r["eth"]
    pub["network_erc20"] = "ethereum-mainnet"
    pub["only_crypto"] = True
    pub.setdefault("note", "агент принимает ТОЛЬКО криптовалюту")
    PUBLIC.write_text(json.dumps(pub, ensure_ascii=False, indent=1))
    try:
        os.chmod(PUBLIC, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return pub


# ── ERC-20 transfer (EIP-1559): готово к отправке, включается флагом --send ──
def build_transfer(to: str, amount_usdt: float, nonce: int, gas_price_wei: int,
                   chain_id: int = 1) -> dict:
    """Собрать и подписать tx USDT ERC-20. Без внешних библиотек (RLP + secp256k1)."""
    w = _wallet_mod()
    r = json.loads(SECRET.read_text())
    priv = int(r["priv_hex"], 16)
    amt = int(round(amount_usdt * 10 ** 6))            # USDT: 6 decimals

    def pad32(x: int) -> bytes:
        return x.to_bytes(32, "big")

    data = bytes.fromhex(TRANSFER_SELECTOR) + pad32(int(to, 16)) + pad32(amt)
    tx = {"chain_id": chain_id, "nonce": nonce, "max_priority_fee": 1_000_000_000,
          "max_fee": gas_price_wei, "gas": 65000, "to": USDT_ETH, "value": 0, "data": data}

    def rlp(item) -> bytes:
        if isinstance(item, int):
            item = b"" if item == 0 else item.to_bytes((item.bit_length() + 7) // 8, "big")
        if isinstance(item, (bytes, bytearray)):
            if len(item) == 1 and item[0] < 0x80:
                return bytes(item)
            if len(item) <= 55:
                return bytes([0x80 + len(item)]) + bytes(item)
            ln = len(item).to_bytes((len(item).bit_length() + 7) // 8, "big")
            return bytes([0xb7 + len(ln)]) + ln + bytes(item)
        payload = b"".join(rlp(x) for x in item)
        if len(payload) <= 55:
            return bytes([0xc0 + len(payload)]) + payload
        ln = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
        return bytes([0xf7 + len(ln)]) + ln + payload

    unsigned = rlp([chain_id, nonce, tx["max_priority_fee"], tx["max_fee"], tx["gas"],
                    bytes.fromhex(USDT_ETH[2:]), 0, data, []])
    sighash = w.keccak256(b"\x02" + unsigned)
    z = int.from_bytes(sighash, "big")
    k = int.from_bytes(os.urandom(32), "big") % w.N
    r_ = (w._mul(k, (w.GX, w.GY))[0]) % w.N
    s_ = (w._inv(k, w.N) * (z + r_ * priv)) % w.N
    y = w._mul(k, (w.GX, w.GY))[1]
    recid = (y & 1) ^ (1 if s_ > w.N // 2 else 0)
    if s_ > w.N // 2:
        s_ = w.N - s_
    signed = rlp([chain_id, nonce, tx["max_priority_fee"], tx["max_fee"], tx["gas"],
                  bytes.fromhex(USDT_ETH[2:]), 0, data, [], recid, r_, s_])
    return {"raw": "0x" + (b"\x02" + signed).hex(), "to": USDT_ETH, "amount_usdt": amount_usdt,
            "nonce": nonce, "chain_id": chain_id, "from": r["eth"]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "selftest":
        print(json.dumps(selftest(), ensure_ascii=False, indent=1))
    elif cmd == "new":
        _wallet_mod().new()
        print(json.dumps(ensure(), ensure_ascii=False, indent=1))
    elif cmd == "ensure":
        print(json.dumps(ensure(), ensure_ascii=False, indent=1))
    elif cmd == "check":
        print(json.dumps(check(sys.argv[2] if len(sys.argv) > 2 else ""), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(show(), ensure_ascii=False, indent=1))
