# SYNTHORA MCP Server

Remote MCP server (JSON-RPC 2.0, streamable HTTP, pure stdlib) exposing the
Hergert Synthora x402 manifest as MCP tools for AI agents.

**Live endpoint:** `https://mcp.hergertsynthora.com/mcp`

## What it does

- `initialize` / `ping` / `tools/list` / `tools/call` over JSON-RPC `POST /mcp`
  (`GET /health` for status; every other route is 404).
- `tools/list` — one tool per manifest entry that declares a supported x402
  contract (`exact`, `eip155:8453`, USDC, v2). The tool name is the published
  id, mapped explicitly to its endpoint; no character substitution. The count
  is a count of tools, not of "active products" (`/health` reports
  `active_products: "UNKNOWN"`).
- `tools/call` — POSTs to the declared endpoint **without payment**. This server
  does not sign payments or hold keys. An HTTP 402 is returned as
  `isError: true`, `structuredContent.code = "payment_required"`, with the
  challenge body unmodified; paying requires an authorised x402 flow outside
  this server. Unknown tool names are rejected with JSON-RPC `-32602`.

Every response declares its sources and is Ed25519-signed by SYNTHORA.

## Discovery

- x402 discovery: https://api.hergertsynthora.com/.well-known/x402.json
- Agent card (A2A): https://api.hergertsynthora.com/.well-known/agent.json
- llms.txt: https://api.hergertsynthora.com/llms.txt
- Official MCP Registry: `com.hergertsynthora/synthora-x402`

## Run your own

```bash
python3 synthora_mcp.py   # serves on 0.0.0.0:8790
python3 -m unittest discover -s tests -v   # local fixtures, no network, no payments
```

MIT licensed.
