#!/usr/bin/env python3
"""synthora_mcp.py — endpoint MCP (JSON-RPC 2.0, streamable HTTP) de SYNTHORA.

Expone el catalogo vivo como herramientas MCP para que agentes que buscan en
registros MCP (smithery, mcp.so, clientes Claude/Cursor) nos encuentren.
tools.call PROXYA al endpoint x402 real: el cobro lo gestiona el paywall x402
de siempre (si no hay pago, el cliente recibe el challenge 402 — los clientes
MCP que entienden x402 pagan; los demas ven el precio).

Sin estado, stdlib pura. systemd: synthora-mcp.service (puerto 8790).
"""
import json, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CATALOG = "https://catalog.hergertsynthora.com/catalog.json"
API = "https://api.hergertsynthora.com"
MAX_TOOLS = 30          # top N por precio (las premium primero)
PROTO = "2025-03-26"

_cache = {"ts": 0, "tools": []}

def tools_vivos():
    import time
    if _cache["tools"] and time.time() - _cache["ts"] < 3600:
        return _cache["tools"]
    try:
        with urllib.request.urlopen(CATALOG, timeout=20) as r:
            cat = json.loads(r.read())
        prods = [p for p in cat.get("products", []) if "/v1/" in (p.get("endpoint") or "")]
        prods.sort(key=lambda p: -float(p.get("price_usd", 0)))
        tools = []
        for p in prods[:MAX_TOOLS]:
            name = p["id"].replace("-", "_")
            tools.append({
                "name": name,
                "description": (p.get("description") or "")[:300],
                "inputSchema": {"type": "object",
                                "properties": {"input": {"type": "string",
                                               "description": "parametros del producto (JSON o texto)"}}},
            })
        _cache.update(ts=time.time(), tools=tools)
    except Exception:
        pass
    return _cache["tools"]


def llamar(tool, args):
    ep = f"{API}/v1/{tool.replace('_', '-')}"
    body = json.dumps(args or {"input": ""}).encode()
    req = urllib.request.Request(ep, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
            return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)[:8000]}]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        if e.code == 402:
            return {"content": [{"type": "text", "text":
                    f"Este servicio cobra por x402 (USDC en Base). Challenge de pago: {raw[:2000]}"}],
                    "isError": False}
        return {"content": [{"type": "text", "text": f"error {e.code}: {raw[:500]}"}], "isError": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"fallo: {type(e).__name__}"}], "isError": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._j({"ok": True, "tools": len(tools_vivos())})
        self._j({"error": "MCP endpoint: POST JSON-RPC"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._j({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "parse error"}}, 400)
        rid = req.get("id")
        m = req.get("method", "")
        if m == "initialize":
            return self._j({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTO,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthora-mcp", "version": "1.0.0"}}})
        if m in ("notifications/initialized", "initialized"):
            return self._j({"jsonrpc": "2.0", "id": rid, "result": {}})
        if m == "tools/list":
            return self._j({"jsonrpc": "2.0", "id": rid,
                            "result": {"tools": tools_vivos()}})
        if m == "tools/call":
            p = req.get("params") or {}
            return self._j({"jsonrpc": "2.0", "id": rid,
                            "result": llamar(p.get("name", ""), p.get("arguments"))})
        if m == "ping":
            return self._j({"jsonrpc": "2.0", "id": rid, "result": {}})
        return self._j({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {m}"}})


if __name__ == "__main__":
    tools_vivos()
    print("synthora-mcp en :8790,", len(_cache["tools"]), "tools")
    ThreadingHTTPServer(("0.0.0.0", 8790), H).serve_forever()
