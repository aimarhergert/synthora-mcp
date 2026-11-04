"""Pruebas locales de synthora_mcp: sin red externa, sin pagos, sin claves.

Ejecutar: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synthora_mcp as m  # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FIXTURE_PAYTO = "0x" + "ab" * 20  # direccion ficticia de prueba


def accept(**kw):
    a = {"scheme": "exact", "network": "eip155:8453", "asset": USDC,
         "payTo": FIXTURE_PAYTO, "amount": "50000", "maxAmountRequired": "50000"}
    a.update(kw)
    return a


def item(resource, accepts=None, **kw):
    d = {"resource": resource, "type": "http", "method": "POST", "x402Version": 2,
         "accepts": [accept()] if accepts is None else accepts, "description": "d"}
    d.update(kw)
    return d


CHALLENGE = {"x402Version": 2, "accepts": [accept()], "error": "payment required"}


def reset_cache():
    m._cache.update(ts=0.0, tools=[], routes={}, rejected=[], entries=0, error=None, source=None)


class Base(unittest.TestCase):
    def setUp(self):
        reset_cache()
        self.posts = []

    def load(self, items):
        with mock.patch.object(m, "_fetch_json", return_value={"items": items}):
            m.refresh_catalog(force=True)

    def rpc(self, method, params=None, rid=1):
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        return m.handle_rpc(req)


class TestInitialize(Base):
    def test_initialize_valido(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        res = r["result"]
        self.assertEqual(res["protocolVersion"], "2025-03-26")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "synthora-mcp")
        self.assertEqual(r["id"], 1)

    def test_initialize_version_desconocida_devuelve_la_soportada(self):
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(r["result"]["protocolVersion"], m.PROTO)

    def test_initialize_sin_params_es_error(self):
        self.assertEqual(self.rpc("initialize")["error"]["code"], -32602)

    def test_notificacion_sin_respuesta(self):
        self.assertIsNone(m.handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_metodo_desconocido(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)

    def test_jsonrpc_invalido(self):
        self.assertEqual(m.handle_rpc({"id": 1, "method": "ping"})["error"]["code"], -32600)


class TestHTTP(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), m.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.p = mock.patch.object(m, "_fetch_json", return_value={"items": [item("https://alpha.hergertsynthora.com/service")]})
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, path, method="GET", body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   data=None if body is None else json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_health(self):
        st, body = self.req("/health")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["catalog_entries"], 1)
        self.assertEqual(d["tools"], 1)
        self.assertEqual(d["active_products"], "UNKNOWN")

    def test_get_ruta_desconocida_404(self):
        self.assertEqual(self.req("/nope")[0], 404)
        self.assertEqual(self.req("/mcp")[0], 404)  # GET a /mcp no es JSON-RPC

    def test_post_ruta_desconocida_404(self):
        self.assertEqual(self.req("/otra", "POST", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 404)

    def test_initialize_por_http(self):
        st, body = self.req("/mcp", "POST", {"jsonrpc": "2.0", "id": 7, "method": "initialize",
                                              "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"], "synthora-mcp")

    def test_notificacion_http_202(self):
        st, body = self.req("/mcp", "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((st, body), (202, b""))

    def test_json_invalido_400(self):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", method="POST", data=b"{no")
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(c.exception.code, 400)


class TestDiscovery(Base):
    def test_id_con_guion_bajo_se_conserva_y_enruta(self):
        url = "https://api.hergertsynthora.com/v1/my_tool"
        self.load([item(url)])
        self.assertEqual([t["name"] for t in m._cache["tools"]], ["my_tool"])
        self.assertEqual(m._cache["routes"]["my_tool"], url)
        with mock.patch.object(m, "_post", return_value=(200, {}, '{"ok": true}')) as p:
            r = self.rpc("tools/call", {"name": "my_tool", "arguments": {"input": "x"}})
        self.assertNotIn("isError", r["result"])
        self.assertEqual(p.call_args[0][0], url)  # URL exacta, sin '_'->'-'

    def test_no_se_convierte_guion_en_guion_bajo(self):
        self.load([item("https://api.hergertsynthora.com/v1/my-tool")])
        self.assertEqual([t["name"] for t in m._cache["tools"]], ["my-tool"])
        r = self.rpc("tools/call", {"name": "my_tool"})  # variante inventada
        self.assertEqual(r["error"]["data"]["code"], "unknown_tool")

    def test_id_desconocido_rechazado_sin_red(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        with mock.patch.object(m, "_post") as p:
            r = self.rpc("tools/call", {"name": "../admin"})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(r["error"]["data"]["code"], "unknown_tool")
        p.assert_not_called()

    def test_entrada_valida_sin_v1(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        self.assertEqual([t["name"] for t in m._cache["tools"]], ["alpha"])

    def test_contrato_incompatible_rechazado(self):
        cases = {
            "unsupported_scheme": [accept(scheme="upto")],
            "unsupported_network": [accept(network="eip155:1")],
            "unsupported_asset": [accept(asset="0x" + "11" * 20)],
            "invalid_payTo": [accept(payTo="no-es-direccion")],
            "invalid_amount": [accept(amount="0", maxAmountRequired="0")],
            "no_accepts": [],
        }
        for reason, acc in cases.items():
            with self.subTest(reason=reason):
                reset_cache()
                self.load([item("https://bad.hergertsynthora.com/service", accepts=acc)])
                self.assertEqual(m._cache["tools"], [])
                self.assertEqual(m._cache["rejected"][0]["reason"], reason)

    def test_version_x402_no_soportada(self):
        self.load([item("https://old.hergertsynthora.com/service", x402Version=1)])
        self.assertEqual(m._cache["rejected"][0]["reason"], "unsupported_x402_version")

    def test_endpoint_fuera_del_dominio_rechazado(self):
        self.load([item("https://evil.example.com/service", id="evil")])
        self.assertEqual(m._cache["rejected"][0]["reason"], "endpoint_outside_allowed_host")

    def test_ids_duplicados_se_retiran_ambos(self):
        self.load([item("https://api.hergertsynthora.com/v1/dup"),
                   item("https://dup.hergertsynthora.com/service")])
        self.assertEqual(m._cache["tools"], [])
        self.assertEqual([r["reason"] for r in m._cache["rejected"]], ["duplicate_id", "duplicate_id"])

    def test_sin_limite_artificial_ni_conteo_de_activos(self):
        n = 45
        self.load([item(f"https://p{i}.hergertsynthora.com/service") for i in range(n)])
        self.assertEqual(len(m._cache["tools"]), n)
        self.assertEqual(m._cache["entries"], n)

    def test_manifiesto_sin_items(self):
        with mock.patch.object(m, "_fetch_json", return_value={"nope": 1}):
            m.refresh_catalog(force=True)
        self.assertEqual(m._cache["tools"], [])

    def test_fallo_de_red_conserva_catalogo_y_lo_declara(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        with mock.patch.object(m, "_fetch_json", side_effect=OSError("x")):
            m.refresh_catalog(force=True)
        self.assertEqual(len(m._cache["tools"]), 1)
        self.assertEqual(m._cache["error"], "OSError")


class TestPayment402(Base):
    def test_402_es_error_con_reto_intacto(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        raw = json.dumps(CHALLENGE, separators=(",", ":"))
        hdr = {"PAYMENT-REQUIRED": "eyJ4Ijoi"}
        with mock.patch.object(m, "_post", return_value=(402, hdr, raw)):
            res = self.rpc("tools/call", {"name": "alpha", "arguments": {"input": "q"}})["result"]
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["code"], "payment_required")
        self.assertEqual(res["content"][0]["text"], raw)            # byte a byte
        self.assertEqual(res["structuredContent"]["challenge_raw"], raw)
        self.assertEqual(res["structuredContent"]["challenge"], CHALLENGE)
        self.assertEqual(res["structuredContent"]["challenge_headers"]["payment-required"], "eyJ4Ijoi")

    def test_402_con_cuerpo_no_json_se_conserva(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        with mock.patch.object(m, "_post", return_value=(402, {}, "pay me")):
            res = self.rpc("tools/call", {"name": "alpha"})["result"]
        self.assertTrue(res["isError"])
        self.assertEqual(res["content"][0]["text"], "pay me")
        self.assertIsNone(res["structuredContent"]["challenge"])

    def test_la_llamada_nunca_envia_cabecera_de_pago(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        captured = {}

        class FakeResp:
            status = 200
            headers = {}
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower() for k in req.headers}
            captured["url"] = req.full_url
            return FakeResp()

        with mock.patch.object(m.urllib.request, "urlopen", fake_urlopen):
            self.rpc("tools/call", {"name": "alpha"})
        self.assertTrue(captured["headers"].isdisjoint({"x-payment", "payment-signature", "authorization"}))
        self.assertEqual(captured["url"], "https://alpha.hergertsynthora.com/service")

    def test_error_upstream_no_es_exito(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        with mock.patch.object(m, "_post", return_value=(502, {}, "bad gateway")):
            res = self.rpc("tools/call", {"name": "alpha"})["result"]
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["code"], "upstream_error")

    def test_upstream_inalcanzable_no_es_exito(self):
        self.load([item("https://alpha.hergertsynthora.com/service")])
        with mock.patch.object(m, "_post", side_effect=OSError("x")):
            res = self.rpc("tools/call", {"name": "alpha"})["result"]
        self.assertTrue(res["isError"])


if __name__ == "__main__":
    unittest.main()
