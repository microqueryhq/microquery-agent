# microquery-agent

Reference agent for the [Microquery](https://microquery.dev) pay-per-query API.

Demonstrates the standard agent lifecycle: register → receive trial credit →
discover databases → run queries → auto top-up balance.

**No ETH required.** Top-ups use an [EIP-2612][] permit signed off-chain; the
operator submits the on-chain transaction.

## See also

[microquery-agent-x402](https://github.com/microqueryhq/microquery-agent-x402) —
the x402 variant: no registration, no web3 dependency, pays via EIP-3009
`TransferWithAuthorization`. Supports a per-query challenge-response mode
(no upfront deposit) and a deposit-first mode. Use this if you want to skip
registration or avoid the permit nonce lookup.

## Prerequisites

- Python 3.11+
- USDC on Base mainnet (for top-ups beyond the $0.10 trial credit)

## Setup

```sh
pip install -r requirements.txt
cp env.example .env
# edit .env — set AGENT_NAME; add WALLET_PRIVATE_KEY to enable top-ups
```

To enable Claude-powered SQL generation:

```sh
pip install anthropic
# set ANTHROPIC_API_KEY in .env
```

## Configuration

| Variable               | Default                                      | Description                                                 |
| ---------------------- | -------------------------------------------- | ----------------------------------------------------------- |
| `AGENT_NAME`           | `microquery-agent`                           | Display name for this agent (max 64 chars)                  |
| `WALLET_PRIVATE_KEY`   | —                                            | Hex private key; required for USDC top-ups beyond trial     |
| `MICROQUERY_BASE_URL`  | `https://microquery.dev`                     | API base URL                                                |
| `RPC_URL`              | `https://mainnet.base.org`                   | Base mainnet JSON-RPC (read-only nonce lookup)              |
| `USDC_ADDRESS`         | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | USDC contract on Base mainnet                               |
| `ESCROW_ADDRESS`       | `0xb1f8eE89bc8E51558a3C2A216620aBa1b7B2d01A` | MicroqueryEscrow contract on Base mainnet                   |
| `TOPUP_AMOUNT_USDC`    | `2`                                          | Amount (USDC) deposited on each top-up (minimum $0.25)      |
| `TOPUP_THRESHOLD_USDC` | `0.50`                                       | Trigger a top-up when balance drops below this              |
| `ANTHROPIC_API_KEY`    | —                                            | Optional; enables Claude-powered query generation           |

## Running

```sh
python agent.py
```

Without `WALLET_PRIVATE_KEY` the agent runs on its 100,000 µUSDC ($0.10) trial
credit and stops with a message when the balance is low.

## How it works

1. **Register** -- `POST /v1/register` creates an account with 100,000 µUSDC
   trial credit ($0.10, ~1,600 typical queries). Returns an `api_key` used for
   all subsequent requests.
2. **Discover** -- `GET /v1/databases` lists available datasets with table
   schemas (SEC EDGAR, NVD/CVE, OSV, Ethereum, Bitcoin, PubMed).
3. **Query** -- `GET /query?database=<name>&query=<sql>` runs SQL with
   `Authorization: Bearer <api_key>`. Response is newline-delimited JSON.
   Cost headers on every response: `X-Microquery-Cost-MicroUSDC` and
   `X-Microquery-Balance-MicroUSDC`.
4. **Top-up** -- when balance falls below `TOPUP_THRESHOLD_USDC`, the agent
   signs an [EIP-2612][] `Permit(owner, spender=escrow, value, nonce, deadline)`
   off-chain and POSTs `{amount, deadline, v, r, s}` to `POST /v1/deposit`.
   The operator calls `depositWithPermit()` on-chain; no ETH is required.

The one read-only web3 call (`usdc.nonces(owner)`) fetches the permit nonce
before signing. It costs no gas.

[EIP-2612]: https://eips.ethereum.org/EIPS/eip-2612
