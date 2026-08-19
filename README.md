# SYNTHORA MCP Server

Remote MCP server (JSON-RPC 2.0, streamable HTTP, pure stdlib) exposing the
SYNTHORA intelligence catalog as 30 MCP tools for AI agents.

**Live endpoint:** `https://mcp.hergertsynthora.com/mcp`

## What it does

- `tools/list` — the top ~30 products of the live SYNTHORA catalog (sorted by
  value): crypto market data, OFAC/sanctions screening, smart-contract safety,
  prediction markets across Polymarket + Kalshi, maritime chokepoint intel,
  WHO health data, macro/FX, weather.
- `tools/call` — proxies the call to the real x402 endpoint. Payment is handled
  by the x402 paywall (USDC on Base): unpaid calls return the standard x402
  payment challenge; x402-aware clients pay per call. Free trial calls per wallet.

Every response declares its sources and is Ed25519-signed by SYNTHORA.

## Discovery

- Catalog: https://catalog.hergertsynthora.com/catalog.json
- x402 discovery: https://api.hergertsynthora.com/.well-known/x402.json
- Agent card (A2A): https://api.hergertsynthora.com/.well-known/agent.json
- llms.txt: https://api.hergertsynthora.com/llms.txt
- Official MCP Registry: `com.hergertsynthora/synthora-x402`

## Run your own

```bash
python3 synthora_mcp.py   # serves on 0.0.0.0:8790
```

MIT licensed.
