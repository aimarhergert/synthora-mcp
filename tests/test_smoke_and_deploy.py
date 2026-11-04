"""Pruebas de scripts/smoke_mcp.py y del ensayo de scripts/deploy_mcp.sh en un sandbox.
Sin red externa, sin pagos, sin systemd real (systemctl simulado), sin secretos.
"""
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

def _sha_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import synthora_mcp as m  # noqa: E402
import smoke_mcp as smoke  # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ACC = {"scheme": "exact", "network": "eip155:8453", "asset": USDC, "payTo": "0x" + "ab" * 20, "amount": "1000"}


def item(url, **kw):
    d = {"resource": url, "method": "POST", "x402Version": 2, "accepts": [ACC], "description": "d"}
    d.update(kw)
    return d


ITEMS = [item("https://alpha.hergertsynthora.com/service"),
         item("https://api.hergertsynthora.com/v1/my-tool"),
         item("https://api.hergertsynthora.com/v1/my_tool")]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSmoke(unittest.TestCase):
    def setUp(self):
        m._cache.update(ts=0.0, tools=[], routes={}, rejected=[], entries=0, error=None, source=None)
        self.p = mock.patch.object(m, "_fetch_json", return_value={"items": ITEMS})
        self.p.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), m.H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.p.stop()

    def test_servidor_correcto_pasa_incluyendo_guion_y_guion_bajo(self):
        rep = smoke.run_checks(self.base, expect_ids=["alpha", "my-tool", "my_tool"])
        failed = [c for c in rep["checks"] if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertTrue(rep["ok"])
        names = [c["name"] for c in rep["checks"]]
        self.assertIn("ID esperado presente tal cual: my_tool", names)
        self.assertIn("ID esperado presente tal cual: my-tool", names)

    def test_id_esperado_ausente_falla(self):
        rep = smoke.run_checks(self.base, expect_ids=["no-publicado"])
        self.assertFalse(rep["ok"])

    def test_call_sin_pago_402_se_trata_como_error(self):
        with mock.patch.object(m, "_post", return_value=(402, {}, '{"x402Version":2}')):
            rep = smoke.run_checks(self.base, call="alpha")
        self.assertTrue(rep["ok"], rep)
        self.assertTrue(any("error tratado como error" in c["name"] for c in rep["checks"]))


class _LaxHandler(BaseHTTPRequestHandler):
    """Servidor defectuoso: responde 200 a todo (el defecto original de rutas)."""
    def log_message(self, *a): pass
    def _ok(self):
        body = b'{"ok": true}'
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = do_POST = lambda self: self._ok()


class TestSmokeNegative(unittest.TestCase):
    def test_servidor_defectuoso_es_detectado(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _LaxHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rep = smoke.run_checks(f"http://127.0.0.1:{srv.server_address[1]}")
            self.assertFalse(rep["ok"])
            failed = {c["name"] for c in rep["checks"] if not c["ok"]}
            self.assertIn("GET ruta desconocida -> 404", failed)
            self.assertIn("POST ruta desconocida -> 404", failed)
        finally:
            srv.shutdown(); srv.server_close()


OLD_STUB = '''import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"ok": True, "version": "old-stub"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("127.0.0.1", int(os.environ["SYNTHORA_MCP_PORT"])), H).serve_forever()
'''

FAKE_SYSTEMCTL = '''#!/usr/bin/env bash
# systemctl simulado: restart/is-active sobre un proceso python en FAKE_PORT
state="$FAKE_STATE"
case "$1" in
  restart)
    [ -f "$state/pid" ] && kill "$(cat "$state/pid")" 2>/dev/null || true
    sleep 0.3
    SYNTHORA_MCP_HOST=127.0.0.1 SYNTHORA_MCP_PORT="$FAKE_PORT" HTTPS_PROXY=http://127.0.0.1:9 \\
      setsid nohup python3 "$FAKE_TARGET" > "$state/svc.log" 2>&1 < /dev/null &
    echo $! > "$state/pid" ;;
  is-active)
    if [ -f "$state/pid" ] && kill -0 "$(cat "$state/pid")" 2>/dev/null; then echo active; else echo inactive; exit 3; fi ;;
esac
'''


class TestDeployRehearsal(unittest.TestCase):
    """Ensayo completo en sandbox: demuestra backup, canary, apply, readback, rollback y reserva."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.target = os.path.join(self.d, "app", "synthora_mcp.py")
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "w") as f:
            f.write(OLD_STUB)
        os.chmod(self.target, 0o644)
        self.state = os.path.join(self.d, "state"); os.makedirs(self.state)
        self.fake = os.path.join(self.d, "fake_systemctl")
        with open(self.fake, "w") as f:
            f.write(FAKE_SYSTEMCTL)
        os.chmod(self.fake, 0o755)
        self.new = os.path.join(ROOT, "synthora_mcp.py")
        self.new_sha = _sha_file(self.new)
        self.port, self.cport = free_port(), free_port()
        self.env = dict(os.environ, TARGET=self.target, NEW_FILE=self.new, EXPECTED_SHA256=self.new_sha,
                        AGENT="rehearsal", SYSTEMCTL=self.fake, PORT=str(self.port), CANARY_PORT=str(self.cport),
                        WORKDIR=os.path.join(self.d, "work"), FAKE_STATE=self.state, FAKE_PORT=str(self.port),
                        FAKE_TARGET=self.target, SMOKE_EXTRA="--allow-empty-catalog",
                        HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9")
        self.script = os.path.join(ROOT, "scripts", "deploy_mcp.sh")

    def tearDown(self):
        pid = os.path.join(self.state, "pid")
        if os.path.exists(pid):
            try:
                with open(pid) as f:
                    os.kill(int(f.read()), 15)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.d, ignore_errors=True)

    def run_cmd(self, cmd, **over):
        env = dict(self.env, **over)
        return subprocess.run(["bash", self.script, cmd], env=env, capture_output=True, text=True, timeout=120)

    def sha(self, p):
        return _sha_file(p)

    def test_ciclo_completo_con_readback_y_rollback(self):
        old_sha = self.sha(self.target)
        subprocess.run([self.fake, "restart", "x"], env=self.env, check=True)   # servicio previo (old)
        r = self.run_cmd("plan"); self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cmd("apply"); self.assertNotEqual(r.returncode, 0)          # sin reserva: no aplica
        self.assertEqual(self.sha(self.target), old_sha)
        r = self.run_cmd("lease"); self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cmd("lease", AGENT="otro"); self.assertNotEqual(r.returncode, 0)  # reserva unica
        r = self.run_cmd("apply"); self.assertNotEqual(r.returncode, 0)          # sin backup: no aplica
        r = self.run_cmd("backup"); self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.env["WORKDIR"], "pre.sha256")) as f:
            self.assertEqual(f.read().strip(), old_sha)
        r = self.run_cmd("canary"); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sha(self.target), old_sha)                         # canary no toca TARGET
        r = self.run_cmd("readback"); self.assertNotEqual(r.returncode, 0)       # aun no aplicado: falla
        r = self.run_cmd("apply"); self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cmd("readback"); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("READBACK OK", r.stdout)
        self.assertEqual(self.sha(self.target), self.new_sha)
        r = self.run_cmd("rollback"); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ROLLBACK OK", r.stdout)
        self.assertEqual(self.sha(self.target), old_sha)
        r = self.run_cmd("release"); self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.env["WORKDIR"], "lease.json")))

    def test_hash_esperado_incorrecto_bloquea_canary_y_apply(self):
        bad = "0" * 64
        self.assertNotEqual(self.run_cmd("canary", EXPECTED_SHA256=bad).returncode, 0)
        self.run_cmd("lease"); self.run_cmd("backup")
        self.assertNotEqual(self.run_cmd("apply", EXPECTED_SHA256=bad).returncode, 0)

    def test_readback_detecta_hash_distinto(self):
        r = self.run_cmd("readback", EXPECTED_SHA256="1" * 64)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
