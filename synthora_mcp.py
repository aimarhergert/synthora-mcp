#!/usr/bin/env python3
"""synthora_mcp.py — endpoint MCP (JSON-RPC 2.0, streamable HTTP) de Hergert Synthora.

Expone el catalogo x402 como herramientas MCP. tools/call hace POST al endpoint
x402 declarado SIN cabecera de pago: este servidor no firma pagos ni custodia
claves. Si el endpoint responde 402, el reto se devuelve intacto como error
(`isError: true`, codigo `payment_required`); pagar es responsabilidad de un
flujo x402 autorizado fuera de este servidor.

Descubrimiento: una entrada del manifiesto se publica como herramienta solo si
declara un `accepts` x402 valido y soportado (ver `x402_acceptance`). El numero
de herramientas NO es un numero de "productos activos": la actividad real no
se puede deducir de este servidor.

Sin estado persistente, stdlib pura. systemd: synthora-mcp.service (puerto 8790).
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.hergertsynthora.com"
MANIFEST = API + "/.well-known/x402.json"
PROTO = "2025-03-26"
SUPPORTED_PROTOS = ("2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "synthora-mcp", "version": "1.1.0",
               "title": "Hergert Synthora x402 MCP Server"}

# Contrato x402 soportado por este servidor (solo para decidir que se lista;
# no se cambia ni se elige ningun payTo).
SUPPORTED_X402_VERSIONS = (2,)
SUPPORTED_SCHEMES = ("exact",)
SUPPORTED_NETWORKS = ("eip155:8453",)
SUPPORTED_ASSETS = ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",)  # USDC en Base
ALLOWED_HOST_SUFFIX = "hergertsynthora.com"
RPC_PATHS = ("/", "/mcp")
MAX_BODY = 1_000_000
CACHE_TTL = 3600

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_lock = threading.Lock()
_cache = {"ts": 0.0, "tools": [], "routes": {}, "rejected": [],
          "entries": 0, "error": None, "source": None}


class ToolError(Exception):
    """Error de herramienta con codigo estable."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- red (inyectable)
def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def _post(url, body, timeout=60):
    """POST JSON sin cabecera de pago. Devuelve (status, headers, texto)."""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- descubrimiento
def x402_acceptance(item):
    """(True, None) si la entrada declara un contrato x402 soportado; si no (False, motivo).

    No elige ni modifica `payTo`: solo exige que exista y tenga forma de direccion.
    """
    if not isinstance(item, dict):
        return False, "entry_not_object"
    if item.get("x402Version") not in SUPPORTED_X402_VERSIONS:
        return False, "unsupported_x402_version"
    accepts = item.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        return False, "no_accepts"
    reason = "no_compatible_accept"
    for a in accepts:
        if not isinstance(a, dict):
            continue
        if a.get("scheme") not in SUPPORTED_SCHEMES:
            reason = "unsupported_scheme"
            continue
        if a.get("network") not in SUPPORTED_NETWORKS:
            reason = "unsupported_network"
            continue
        asset = str(a.get("asset", ""))
        if asset.lower() not in [x.lower() for x in SUPPORTED_ASSETS]:
            reason = "unsupported_asset"
            continue
        if not _ADDR_RE.match(str(a.get("payTo", ""))):
            reason = "invalid_payTo"
            continue
        amount = str(a.get("amount", a.get("maxAmountRequired", "")))
        if not (amount.isdigit() and int(amount) > 0):
            reason = "invalid_amount"
            continue
        return True, None
    return False, reason


def _endpoint_ok(url):
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and bool(host) and
            (host == ALLOWED_HOST_SUFFIX or host.endswith("." + ALLOWED_HOST_SUFFIX)))


def derive_id(item):
    """Nombre publicado de la herramienta. Sin sustituir caracteres: o es valido tal cual o None."""
    explicit = item.get("id")
    if isinstance(explicit, str):
        return explicit if _ID_RE.match(explicit) else None
    u = urllib.parse.urlparse(str(item.get("resource", "")))
    host = (u.hostname or "").lower()
    path = u.path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    cand = None
    if len(parts) == 2 and parts[0] == "v1":
        cand = parts[1]
    elif not parts or parts == ["service"]:
        first = host.split(".")[0]
        cand = first if host != urllib.parse.urlparse(API).hostname else None
    return cand if cand and _ID_RE.match(cand) else None


def build_tools(manifest):
    """Devuelve (tools, routes, rejected, entries). routes: nombre -> endpoint explicito."""
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        return [], {}, [{"id": None, "reason": "manifest_without_items"}], 0
    tools, routes, rejected, seen = [], {}, [], {}
    for item in items:
        ok, reason = x402_acceptance(item)
        rid = item.get("resource") if isinstance(item, dict) else None
        if not ok:
            rejected.append({"resource": rid, "reason": reason})
            continue
        if item.get("method", "POST") != "POST":
            rejected.append({"resource": rid, "reason": "unsupported_method"})
            continue
        if not _endpoint_ok(rid):
            rejected.append({"resource": rid, "reason": "endpoint_outside_allowed_host"})
            continue
        name = derive_id(item)
        if not name:
            rejected.append({"resource": rid, "reason": "no_valid_id"})
            continue
        if name in seen:
            seen[name].append(rid)
            continue
        seen[name] = [rid]
        tools.append({
            "name": name,
            "description": (item.get("description") or "")[:300],
            "inputSchema": {"type": "object",
                            "properties": {"input": {"type": "string",
                                           "description": "parametros del producto (JSON o texto)"}}},
        })
        routes[name] = rid
    # Ids duplicados: ambiguos, se retiran todos (no se adivina cual es el correcto).
    for name, rids in seen.items():
        if len(rids) > 1:
            tools[:] = [t for t in tools if t["name"] != name]
            routes.pop(name, None)
            for r in rids:
                rejected.append({"resource": r, "reason": "duplicate_id"})
    return tools, routes, rejected, len(items)


def refresh_catalog(force=False):
    with _lock:
        if not force and _cache["ts"] and time.time() - _cache["ts"] < CACHE_TTL:
            return
        try:
            tools, routes, rejected, entries = build_tools(_fetch_json(MANIFEST))
            _cache.update(ts=time.time(), tools=tools, routes=routes,
                          rejected=rejected, entries=entries, error=None, source=MANIFEST)
        except Exception as e:  # conserva el ultimo catalogo bueno, pero lo declara
            _cache["error"] = type(e).__name__
            _cache["ts"] = time.time() - CACHE_TTL + 60  # sin reintento en bucle: proximo intento en 60 s
    return


def tools_vivos():
    refresh_catalog()
    return _cache["tools"]


# ---------------------------------------------------------------- llamadas
def llamar(tool, args):
    refresh_catalog()
    endpoint = _cache["routes"].get(tool) if isinstance(tool, str) else None
    if endpoint is None:
        raise ToolError("unknown_tool", f"unknown tool: {str(tool)[:80]!r}")
    body = json.dumps(args or {"input": ""}).encode()
    try:
        status, headers, raw = _post(endpoint, body)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"fallo: {type(e).__name__}"}],
                "isError": True,
                "structuredContent": {"code": "upstream_unreachable"}}
    if status == 402:
        try:
            challenge = json.loads(raw)
        except Exception:
            challenge = None
        lh = {k.lower(): v for k, v in headers.items()}
        sc = {"code": "payment_required", "http_status": 402,
              "challenge": challenge, "challenge_raw": raw}
        for h in ("payment-required", "x-payment-required"):
            if h in lh:
                sc.setdefault("challenge_headers", {})[h] = lh[h]
        return {"content": [{"type": "text", "text": raw}],
                "isError": True, "structuredContent": sc}
    if 200 <= status < 300:
        try:
            text = json.dumps(json.loads(raw), ensure_ascii=False)[:8000]
        except Exception:
            text = raw[:8000]
        return {"content": [{"type": "text", "text": text}]}
    return {"content": [{"type": "text", "text": f"error {status}: {raw[:500]}"}],
            "isError": True,
            "structuredContent": {"code": "upstream_error", "http_status": status}}


# ---------------------------------------------------------------- JSON-RPC
def _err(rid, code, message, data=None):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


def handle_rpc(req):
    """Devuelve el objeto de respuesta, o None si es una notificacion."""
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or \
            not isinstance(req.get("method"), str):
        return _err(req.get("id") if isinstance(req, dict) else None, -32600, "invalid request")
    rid = req.get("id")
    m = req["method"]
    if "id" not in req:  # notificacion: sin respuesta
        return None
    params = req.get("params")
    if m == "initialize":
        if not isinstance(params, dict):
            return _err(rid, -32602, "initialize requires params object")
        asked = params.get("protocolVersion")
        if not isinstance(asked, str):
            return _err(rid, -32602, "protocolVersion required")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked if asked in SUPPORTED_PROTOS else PROTO,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO}}
    if m == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools_vivos()}}
    if m == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _err(rid, -32602, "params.name required")
        args = params.get("arguments")
        if args is not None and not isinstance(args, dict):
            return _err(rid, -32602, "params.arguments must be an object")
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": llamar(params["name"], args)}
        except ToolError as e:
            return _err(rid, -32602, e.message, {"code": e.code})
    return _err(rid, -32601, f"method not found: {m[:80]}")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        data = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            refresh_catalog()
            return self._send(200, {
                "ok": True,
                "catalog_entries": _cache["entries"],
                "tools": len(_cache["tools"]),
                "rejected": len(_cache["rejected"]),
                "catalog_error": _cache["error"],
                "active_products": "UNKNOWN"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path not in RPC_PATHS:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n < 0 or n > MAX_BODY:
                raise ValueError
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, _err(None, -32700, "parse error"))
        resp = handle_rpc(req)
        if resp is None:
            return self._send(202)
        self._send(200, resp)


if __name__ == "__main__":
    refresh_catalog(force=True)
    print("synthora-mcp en :8790,", len(_cache["tools"]), "tools de",
          _cache["entries"], "entradas del catalogo")
    ThreadingHTTPServer(("0.0.0.0", 8790), H).serve_forever()
