#!/usr/bin/env python3
"""
microquery-agent -- reference implementation of the Microquery agent lifecycle.

Lifecycle:
    1. Register an Ethereum wallet address -> receive account_id
    2. Deposit USDC into the MicroqueryEscrow contract
    3. Discover available databases via GET /v1/databases
    4. Run queries; track cost from X-Microquery-Cost-MicroUSDC response header
    5. Top up balance when it falls below TOPUP_THRESHOLD_USDC
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Any

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("MICROQUERY_BASE_URL", "https://api.microquery.dev")
PRIVATE_KEY = os.environ["WALLET_PRIVATE_KEY"]
RPC_URL = os.environ.get("RPC_URL", "https://sepolia.base.org")
TOPUP_AMOUNT_USDC = float(os.environ.get("TOPUP_AMOUNT_USDC", "2"))
TOPUP_THRESHOLD_USDC = float(os.environ.get("TOPUP_THRESHOLD_USDC", "0.50"))

CHAIN_ID = 84532  # Base Sepolia
USDC_ADDRESS = Web3.to_checksum_address(
    os.environ.get("USDC_ADDRESS", "0x036CbD53842c5426634e7929541eC2318f3dCF7e")
)
ESCROW_ADDRESS = Web3.to_checksum_address(
    os.environ.get("ESCROW_ADDRESS", "0x0000000000000000000000000000000000000000")
)

MICRO_USDC_PER_USDC = 1_000_000  # USDC has 6 decimals

# ---------------------------------------------------------------------------
# ABIs
# ---------------------------------------------------------------------------

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

ESCROW_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "accountId", "type": "bytes32"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]

# ---------------------------------------------------------------------------
# EIP-712 request signing
# ---------------------------------------------------------------------------

_EIP712_DOMAIN = {
    "name": "Microquery",
    "version": "1",
    "chainId": CHAIN_ID,
}

_EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "ApiRequest": [
        {"name": "accountId", "type": "string"},
        {"name": "method", "type": "string"},
        {"name": "path", "type": "string"},
        {"name": "bodyHash", "type": "bytes32"},
        {"name": "timestamp", "type": "uint256"},
    ],
}


def _sign_request(
    account_id: str, method: str, path: str, body: bytes, private_key: str
) -> tuple[str, int]:
    """Sign an API request with EIP-712; return (hex_signature, unix_timestamp)."""
    timestamp = int(time.time())
    body_hash = hashlib.sha256(body).digest()  # 32 bytes

    structured_data = {
        "types": _EIP712_TYPES,
        "domain": _EIP712_DOMAIN,
        "primaryType": "ApiRequest",
        "message": {
            "accountId": account_id,
            "method": method.upper(),
            "path": path,
            "bodyHash": body_hash,
            "timestamp": timestamp,
        },
    }

    signable = encode_typed_data(full_message=structured_data)
    signed = Account.sign_message(signable, private_key=private_key)
    return signed.signature.hex(), timestamp


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class MicroqueryClient:
    def __init__(self, base_url: str, private_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.account = Account.from_key(private_key)
        self._private_key = private_key
        self.account_id: str | None = None

    def _auth_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        if not self.account_id:
            raise RuntimeError("call register() before making authenticated requests")
        sig, ts = _sign_request(self.account_id, method, path, body, self._private_key)
        return {
            "Authorization": f"EIP712 {self.account_id}.{ts}.{sig}",
            "Content-Type": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        authenticated: bool = True,
    ) -> requests.Response:
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = (
            self._auth_headers(method, path, body)
            if authenticated
            else {"Content-Type": "application/json"}
        )
        resp = requests.request(
            method,
            self.base_url + path,
            data=body or None,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp

    def register(self) -> str:
        """Register the wallet and store the returned account_id."""
        resp = self._call(
            "POST",
            "/v1/register",
            {"wallet_address": self.account.address},
            authenticated=False,
        )
        self.account_id = resp.json()["account_id"]
        print(f"registered: account_id={self.account_id}  wallet={self.account.address}")
        return self.account_id

    def databases(self) -> list[dict]:
        """Return available databases and their schemas."""
        return self._call("GET", "/v1/databases").json()

    def query(self, database_id: str, sql: str) -> tuple[dict, float, float]:
        """Run a SQL query; return (result, cost_usdc, balance_usdc)."""
        resp = self._call("POST", "/v1/query", {"database_id": database_id, "sql": sql})
        cost = int(resp.headers.get("X-Microquery-Cost-MicroUSDC", 0)) / MICRO_USDC_PER_USDC
        balance = int(resp.headers.get("X-Microquery-Balance-MicroUSDC", 0)) / MICRO_USDC_PER_USDC
        return resp.json(), cost, balance


# ---------------------------------------------------------------------------
# Blockchain: USDC deposit into MicroqueryEscrow
# ---------------------------------------------------------------------------


def _account_id_to_bytes32(account_id: str) -> bytes:
    """Convert an account_id to a 32-byte value for the escrow contract."""
    raw = account_id.lstrip("0x")
    if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return bytes.fromhex(raw)
    # Non-hex account_id (e.g. UUID): hash it to bytes32
    return hashlib.sha256(account_id.encode()).digest()


def deposit_usdc(w3: Web3, account: Any, account_id: str, amount_usdc: float) -> None:
    """Approve USDC to the escrow contract and call MicroqueryEscrow.deposit()."""
    amount_raw = int(amount_usdc * MICRO_USDC_PER_USDC)
    account_id_b32 = _account_id_to_bytes32(account_id)

    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
    escrow = w3.eth.contract(address=ESCROW_ADDRESS, abi=ESCROW_ABI)

    nonce = w3.eth.get_transaction_count(account.address)

    tx = usdc.functions.approve(ESCROW_ADDRESS, amount_raw).build_transaction(
        {"from": account.address, "nonce": nonce, "chainId": CHAIN_ID}
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"approved {amount_usdc} USDC  tx={tx_hash.hex()}")

    tx = escrow.functions.deposit(account_id_b32, amount_raw).build_transaction(
        {"from": account.address, "nonce": nonce + 1, "chainId": CHAIN_ID}
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"deposited {amount_usdc} USDC  tx={tx_hash.hex()}")


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
    client = MicroqueryClient(BASE_URL, PRIVATE_KEY)
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    # 1. Register
    client.register()

    # 2. Initial deposit
    deposit_usdc(w3, client.account, client.account_id, TOPUP_AMOUNT_USDC)

    # 3. Discover databases
    dbs = client.databases()
    if not dbs:
        print("no databases available")
        sys.exit(0)
    print(f"found {len(dbs)} database(s)")

    # 4. Query loop
    balance = TOPUP_AMOUNT_USDC
    for db in dbs:
        sql = _pick_sql(db)
        print(f"\ndatabase={db.get('id')}  sql={sql!r}")

        result, cost, balance = client.query(db["id"], sql)
        print(
            f"  rows={len(result.get('rows', []))}  "
            f"cost={cost:.6f} USDC  balance={balance:.6f} USDC"
        )

        # 5. Top up if balance is low
        if balance < TOPUP_THRESHOLD_USDC:
            print(
                f"balance {balance:.6f} USDC is below threshold "
                f"{TOPUP_THRESHOLD_USDC} USDC -- depositing {TOPUP_AMOUNT_USDC} USDC"
            )
            deposit_usdc(w3, client.account, client.account_id, TOPUP_AMOUNT_USDC)
            balance += TOPUP_AMOUNT_USDC


if __name__ == "__main__":
    main()
