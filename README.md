# microquery-agent

Reference agent for the [Microquery](https://microquery.dev) pay-per-query API.

Demonstrates the full agent lifecycle: register a wallet → deposit USDC →
discover databases → run queries → auto top-up balance.

## Prerequisites

- Python 3.11+
- An Ethereum wallet private key
- Base Sepolia ETH (gas) and USDC (testnet faucet at
  [faucet.circle.com](https://faucet.circle.com))
- The `MicroqueryEscrow` contract address (provided at sign-up)

## Setup

```sh
pip install -r requirements.txt
cp env.example .env
# edit .env — at minimum set WALLET_PRIVATE_KEY and ESCROW_ADDRESS
```

To enable Claude-powered SQL generation, also install the Anthropic SDK:

```sh
pip install anthropic
```

## Configuration

| Variable               | Default                                      | Description                                       |
| ---------------------- | -------------------------------------------- | ------------------------------------------------- |
| `WALLET_PRIVATE_KEY`   | **required**                                 | Hex private key for the signing wallet            |
| `ESCROW_ADDRESS`       | **required**                                 | `MicroqueryEscrow` contract address on Base Sepolia |
| `MICROQUERY_BASE_URL`  | `https://api.microquery.dev`                 | API base URL                                      |
| `RPC_URL`              | `https://sepolia.base.org`                   | Base Sepolia JSON-RPC endpoint                    |
| `USDC_ADDRESS`         | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` | Base Sepolia USDC contract                        |
| `TOPUP_AMOUNT_USDC`    | `2`                                          | Amount (USDC) deposited on each top-up            |
| `TOPUP_THRESHOLD_USDC` | `0.50`                                       | Trigger a top-up when balance drops below this    |
| `ANTHROPIC_API_KEY`    | —                                            | Optional; enables Claude-powered query generation |

## Running

```sh
python agent.py
```

## How it works

1. **Register** -- `POST /v1/register` links the wallet address to an
   account ID stored for subsequent requests.
2. **Deposit** -- approves USDC to the `MicroqueryEscrow` contract, then
   calls `deposit(accountId, amount)` on-chain.
3. **Discover** -- `GET /v1/databases` returns available datasets with their
   table schemas.
4. **Query** -- `POST /v1/query` runs a SQL statement. The response includes
   `X-Microquery-Cost-MicroUSDC` and `X-Microquery-Balance-MicroUSDC` headers
   (1 USDC = 1 000 000 micro-USDC).
5. **Top-up** -- when the balance falls below `TOPUP_THRESHOLD_USDC`, a new
   deposit is made automatically before the next query.

All API requests after registration are authenticated with an [EIP-712][]
signature over the request method, path, SHA-256 body hash, and a Unix
timestamp, sent as:

```
Authorization: EIP712 <account_id>.<timestamp>.<hex_signature>
```

## Claude integration

If `ANTHROPIC_API_KEY` is set and `anthropic` is installed, the agent calls
`claude-sonnet-4-6` to generate a SQL query tailored to each database's
schema. Without it the agent falls back to `SELECT * FROM <table> LIMIT 5`.

[EIP-712]: https://eips.ethereum.org/EIPS/eip-712
