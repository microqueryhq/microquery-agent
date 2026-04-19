#!/usr/bin/env python3
"""
microquery-agent -- reference implementation of the Microquery agent lifecycle.

Lifecycle:
    1. Register (POST /v1/register) -> api_key + 100,000 µUSDC trial credit ($0.10)
    2. Discover databases (GET /v1/databases)
    3. Run queries via GET /query (Bearer token auth)
    4. Track cost from X-Microquery-* headers; top up via EIP-2612 permit when low
       (sign Permit off-chain; operator submits depositWithPermit on-chain — no ETH needed)
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("MICROQUERY_BASE_URL", "https://microquery.dev")
AGENT_NAME = os.environ.get("AGENT_NAME", "microquery-agent")
PRIVATE_KEY = os.environ.get("WALLET_PRIVATE_KEY")  # optional; required for top-ups
RPC_URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
TOPUP_AMOUNT_USDC = float(os.environ.get("TOPUP_AMOUNT_USDC", "2"))
TOPUP_THRESHOLD_USDC = float(os.environ.get("TOPUP_THRESHOLD_USDC", "0.50"))

CHAIN_ID = 8453  # Base mainnet
USDC_ADDRESS = os.environ.get(
    "USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
)
ESCROW_ADDRESS = os.environ.get(
    "ESCROW_ADDRESS", "0xb1f8eE89bc8E51558a3C2A216620aBa1b7B2d01A"
)

MICRO_USDC_PER_USDC = 1_000_000  # USDC has 6 decimals

# ---------------------------------------------------------------------------
# EIP-2612 permit deposit
# ---------------------------------------------------------------------------

_USDC_NONCES_ABI = [
    {
        "name": "nonces",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


def _usdc_permit_nonce(owner_addr: str) -> int:
    """Fetch the owner's current EIP-2612 permit nonce from the USDC contract."""
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=_USDC_NONCES_ABI
    )
    return usdc.functions.nonces(Web3.to_checksum_address(owner_addr)).call()


def _sign_permit(
    owner: str, value: int, nonce: int, deadline: int, private_key: str
) -> tuple[int, str, str]:
    """
    Sign an EIP-2612 Permit for the MicroqueryEscrow spender.
    Returns (v, r_hex, s_hex). No on-chain transaction or ETH is required.
    """
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    structured_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Permit": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "domain": {
            "name": "USD Coin",
            "version": "2",
            "chainId": CHAIN_ID,
            "verifyingContract": USDC_ADDRESS,
        },
        "primaryType": "Permit",
        "message": {
            "owner": owner,
            "spender": ESCROW_ADDRESS,
            "value": value,
            "nonce": nonce,
            "deadline": deadline,
        },
    }

    signable = encode_typed_data(full_message=structured_data)
    signed = Account.sign_message(signable, private_key=private_key)
    sig = bytes(signed.signature)
    return signed.v, "0x" + sig[:32].hex(), "0x" + sig[32:64].hex()


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class MicroqueryClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key: str | None = None
        self.account_id: str | None = None
        self._wallet_addr: str | None = None

    def register(self, name: str, wallet_addr: str | None = None) -> dict:
        """Register and store api_key; returns full registration response."""
        payload: dict[str, Any] = {"name": name}
        if wallet_addr:
            payload["wallet_addr"] = wallet_addr
        resp = requests.post(f"{self.base_url}/v1/register", json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self.api_key = data["api_key"]
        self.account_id = data["id"]
        self._wallet_addr = data.get("wallet_addr")
        balance_usdc = data["balance"] / MICRO_USDC_PER_USDC
        print(f"registered: id={self.account_id}  balance={balance_usdc:.4f} USDC trial credit")
        return data

    def databases(self) -> list[dict]:
        """Return available databases with table schemas."""
        resp = requests.get(f"{self.base_url}/v1/databases", timeout=30)
        resp.raise_for_status()
        return resp.json().get("databases", [])

    def query(self, database: str, sql: str) -> tuple[list[dict], float, float]:
        """
        Run a SQL query; return (rows, cost_usdc, balance_usdc).
        Response is newline-delimited JSON (one object per row).
        """
        resp = requests.get(
            f"{self.base_url}/query",
            params={"database": database, "query": sql},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60,
        )
        resp.raise_for_status()
        cost = int(resp.headers.get("X-Microquery-Cost-MicroUSDC", 0)) / MICRO_USDC_PER_USDC
        balance = int(resp.headers.get("X-Microquery-Balance-MicroUSDC", 0)) / MICRO_USDC_PER_USDC
        rows = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
        return rows, cost, balance

    def deposit(self, amount_usdc: float) -> None:
        """
        Deposit USDC via EIP-2612 permit (no ETH gas required from agent).
        Requires a linked wallet; operator submits the depositWithPermit tx.
        """
        if not self._wallet_addr or not PRIVATE_KEY:
            raise RuntimeError("deposit requires a linked wallet (set WALLET_PRIVATE_KEY)")

        amount_raw = int(amount_usdc * MICRO_USDC_PER_USDC)
        deadline = int(time.time()) + 3600
        nonce = _usdc_permit_nonce(self._wallet_addr)
        v, r, s = _sign_permit(self._wallet_addr, amount_raw, nonce, deadline, PRIVATE_KEY)

        resp = requests.post(
            f"{self.base_url}/v1/deposit",
            json={"amount": amount_raw, "deadline": deadline, "v": v, "r": r, "s": s},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        balance_usdc = data.get("balance", 0) / MICRO_USDC_PER_USDC
        print(
            f"deposited {amount_usdc} USDC  "
            f"balance={balance_usdc:.4f} USDC  tx={data.get('tx_hash')}"
        )


# ---------------------------------------------------------------------------
# Query generation: Claude if available, otherwise a safe default
# ---------------------------------------------------------------------------

_FALLBACK_SQL = "SELECT * FROM {table} LIMIT 5"


def _pick_sql(database: dict) -> str:
    """Return a SQL query for the database, using Claude if ANTHROPIC_API_KEY is set."""
    try:
        import anthropic
    except ImportError:
        pass
    else:
        client = anthropic.Anthropic()
        schema = json.dumps(database, indent=2)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Database schema:\n\n{schema}\n\n"
                        "Write one interesting SQL SELECT query returning at most 10 rows. "
                        "Return only the SQL, no commentary."
                    ),
                }
            ],
        )
        return msg.content[0].text.strip()

    tables = database.get("tables", [])
    table = tables[0]["name"] if tables else "events"
    return _FALLBACK_SQL.format(table=table)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    wallet_addr: str | None = None
    if PRIVATE_KEY:
        from eth_account import Account
        wallet_addr = Account.from_key(PRIVATE_KEY).address

    client = MicroqueryClient(BASE_URL)

    # 1. Register (trial credit: 100,000 µUSDC = $0.10, ~1,600 typical queries)
    client.register(AGENT_NAME, wallet_addr)

    # 2. Discover databases
    dbs = client.databases()
    if not dbs:
        print("no databases available")
        sys.exit(0)
    print(f"found {len(dbs)} database(s): {', '.join(db['name'] for db in dbs)}")

    # 3. Query loop
    for db in dbs:
        sql = _pick_sql(db)
        print(f"\ndatabase={db['name']}  sql={sql!r}")

        rows, cost, balance = client.query(db["name"], sql)
        print(f"  rows={len(rows)}  cost={cost:.6f} USDC  balance={balance:.6f} USDC")

        # 4. Top up if balance is low
        if balance < TOPUP_THRESHOLD_USDC:
            if not PRIVATE_KEY:
                print(
                    f"balance {balance:.6f} USDC is low -- "
                    "set WALLET_PRIVATE_KEY to enable auto top-up"
                )
                break
            print(
                f"balance {balance:.6f} USDC is below threshold "
                f"{TOPUP_THRESHOLD_USDC} USDC -- depositing {TOPUP_AMOUNT_USDC} USDC"
            )
            client.deposit(TOPUP_AMOUNT_USDC)


if __name__ == "__main__":
    main()
