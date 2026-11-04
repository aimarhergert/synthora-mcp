#!/usr/bin/env python3
"""smoke_mcp.py — comprobaciones de humo de un servidor synthora-mcp ya levantado.

Solo hace GET/POST de lectura al propio servidor: NO paga, NO firma, NO usa secretos.
Con --call NAME envia UNA peticion sin pago al endpoint de ese producto (obtiene el reto 402
o una respuesta; no es una compra).

Uso:
  python3 scripts/smoke_mcp.py --base http://127.0.0.1:8790 --expect-ids alpha,my-tool,my_tool
  python3 scripts/smoke_mcp.py --base https://mcp.hergertsynthora.com --no-health   # via proxy
Salida: JSON; codigo 0 si todo ok, 1 si algo falla, 2 si el servidor no responde.
"""
import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def http(base, method, path, body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def rpc(base, rpc_path, method, params=None, rid=1):
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        req["params"] = params
    st, txt = http(base, "POST", rpc_path, req)
    try:
        return st, json.loads(txt) if txt else None
    except ValueError:
        return st, None


def run_checks(base, rpc_path="/mcp", expect_ids=(), allow_empty_catalog=False,
               no_health=False, call=None):
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})

    if not no_health:
        st, txt = http(base, "GET", "/health")
        try:
            h = json.loads(txt)
        except ValueError:
            h = {}
        add("GET /health -> 200 con ok=true", st == 200 and h.get("ok") is True, f"status={st}")
        add("/health no afirma productos activos", h.get("active_products") == "UNKNOWN",
            h.get("active_products"))
        if not allow_empty_catalog:
            add("catalogo cargado (sin catalog_error y tools>0)",
                not h.get("catalog_error") and (h.get("tools") or 0) > 0,
                f"tools={h.get('tools')} catalog_error={h.get('catalog_error')}")
    st, _ = http(base, "GET", "/__ruta_inexistente__")
    add("GET ruta desconocida -> 404", st == 404, f"status={st}")
    st, _ = http(base, "POST", "/__ruta_inexistente__", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    add("POST ruta desconocida -> 404", st == 404, f"status={st}")

    st, r = rpc(base, rpc_path, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
    res = (r or {}).get("result") or {}
    add("initialize JSON-RPC valido (identidad + capacidades)",
        st == 200 and res.get("serverInfo", {}).get("name") == "synthora-mcp" and "tools" in res.get("capabilities", {}),
        f"status={st} serverInfo={res.get('serverInfo')}")
    st, _ = http(base, "POST", rpc_path, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    add("notificacion initialized -> 202", st == 202, f"status={st}")
    st, r = rpc(base, rpc_path, "initialize")
    add("initialize sin params -> error -32602", ((r or {}).get("error") or {}).get("code") == -32602, f"status={st}")

    st, r = rpc(base, rpc_path, "tools/list")
    tools = [t.get("name") for t in ((r or {}).get("result") or {}).get("tools", [])]
    add("tools/list responde", st == 200 and r is not None, f"tools={len(tools)}")
    if tools or not allow_empty_catalog:
        add("todos los nombres son IDs validos (sin alterar)", all(isinstance(n, str) and ID_RE.match(n) for n in tools),
            [n for n in tools if not (isinstance(n, str) and ID_RE.match(n))][:5])
    for want in expect_ids:
        add(f"ID esperado presente tal cual: {want}", want in tools)
    st, r = rpc(base, rpc_path, "tools/call", {"name": "__no_such_tool__", "arguments": {}})
    err = (r or {}).get("error") or {}
    add("ID desconocido rechazado (-32602 unknown_tool) sin llamada externa",
        err.get("code") == -32602 and (err.get("data") or {}).get("code") == "unknown_tool", f"status={st}")
    for want in expect_ids:  # variante con guion<->guion bajo invertido no debe resolverse si no esta publicada
        alt = want.replace("-", "_") if "-" in want else want.replace("_", "-")
        if alt != want and alt not in tools:
            st, r = rpc(base, rpc_path, "tools/call", {"name": alt, "arguments": {}})
            add(f"variante no publicada rechazada: {alt}",
                (((r or {}).get("error") or {}).get("data") or {}).get("code") == "unknown_tool")
    if call:
        st, r = rpc(base, rpc_path, "tools/call", {"name": call, "arguments": {"input": ""}})
        res = (r or {}).get("result") or {}
        sc = res.get("structuredContent") or {}
        if res.get("isError"):
            add(f"{call}: error tratado como error (nunca exito)", bool(sc.get("code")), f"code={sc.get('code')}")
        else:
            add(f"{call}: respuesta sin pago", True, "NO prueba entrega pagada ni aceptacion")
    ok = all(c["ok"] for c in checks)
    return {"checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "base": base, "ok": ok, "checks": checks,
            "scope": "smoke tecnico; no es venta, entrega pagada ni aceptacion del consumidor"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--rpc-path", default="/mcp")
    ap.add_argument("--expect-ids", default="", help="IDs separados por coma que deben estar en tools/list")
    ap.add_argument("--allow-empty-catalog", action="store_true", help="ensayo sin red: no exige catalogo")
    ap.add_argument("--no-health", action="store_true", help="el proxy publico puede no enrutar /health")
    ap.add_argument("--call", help="producto a pedir SIN pago (obtiene reto 402 o respuesta)")
    a = ap.parse_args(argv)
    try:
        rep = run_checks(a.base, a.rpc_path, [x for x in a.expect_ids.split(",") if x],
                         a.allow_empty_catalog, a.no_health, a.call)
    except (urllib.error.URLError, OSError) as e:
        print(json.dumps({"ok": False, "error": f"unreachable: {type(e).__name__}", "base": a.base}))
        return 2
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
