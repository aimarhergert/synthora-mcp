# Runbook: desplegar `synthora_mcp.py` (Hergert Synthora MCP) — ejecuta el propietario

Estado que alcanza: **despliegue con readback técnico**. NO demuestra venta, entrega pagada ni aceptación del
consumidor. Nada de lo de abajo paga, firma ni usa secretos. El ensayo completo de estos pasos (con `systemctl`
simulado) está en `tests/test_smoke_and_deploy.py` y corre en CI.

## Valores de esta versión
| Dato | Valor |
|---|---|
| Archivo nuevo | `synthora_mcp.py` de `master` (repo `aimarhergert/synthora-mcp`) |
| sha256 esperado | `4950089455f9c34142b319117aabccac527ec3802ea6554b48b13dff921f8de6` |
| Cambios respecto al anterior | `initialize` JSON-RPC, rutas desconocidas 404 (POST solo en `/` y `/mcp`), mapa explícito nombre→endpoint, descubrimiento por contrato x402 desde `/.well-known/x402.json`, 402 como `isError:true`/`payment_required`, `SYNTHORA_MCP_HOST`/`SYNTHORA_MCP_PORT` |

> Si el hash que obtienes de GitHub no es exactamente ese, **detente**: no es la versión revisada.

## 0. Preparación (solo lectura, en el host)
```bash
# Localiza el archivo instalado y el servicio
systemctl cat synthora-mcp.service | grep -E '^(ExecStart|WorkingDirectory)'
export TARGET=/RUTA/REAL/synthora_mcp.py        # sale de ExecStart/WorkingDirectory
export SERVICE=synthora-mcp.service PORT=8790
export AGENT="tu-nombre-o-id"                   # quién toma la reserva
export WORKDIR=/var/tmp/synthora-mcp-deploy
sha256sum "$TARGET"; systemctl is-active "$SERVICE"
# Línea base del proxy ANTES de tocar nada (anótala)
curl -sS -o /dev/null -w 'proxy /mcp ping: %{http_code}\n' -X POST https://mcp.hergertsynthora.com/mcp \
     -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
curl -sS http://127.0.0.1:$PORT/health; echo
```

## 1. Obtener y verificar el archivo y los scripts
```bash
mkdir -p "$WORKDIR" && cd "$WORKDIR"
BASE=https://raw.githubusercontent.com/aimarhergert/synthora-mcp/master
curl -fsSL $BASE/synthora_mcp.py          -o synthora_mcp.new
curl -fsSL $BASE/scripts/smoke_mcp.py     -o smoke_mcp.py
curl -fsSL $BASE/scripts/deploy_mcp.sh    -o deploy_mcp.sh && chmod +x deploy_mcp.sh
export NEW_FILE="$WORKDIR/synthora_mcp.new"
export EXPECTED_SHA256=4950089455f9c34142b319117aabccac527ec3802ea6554b48b13dff921f8de6
[ "$(sha256sum "$NEW_FILE" | cut -d' ' -f1)" = "$EXPECTED_SHA256" ] && echo HASH_OK || echo HASH_DISTINTO_DETENTE
export SMOKE="$WORKDIR/smoke_mcp.py"
# IDs reales con guion y con guion bajo que quieras comprobar (de tu manifiesto), p. ej.:
export EXPECT_IDS="agentcard,mi-id-con-guion,mi_id_con_guion_bajo"   # ajústalo; vacío = no comprobar
```

## 2. Un cambio cada vez
```bash
./deploy_mcp.sh plan        # no cambia nada
./deploy_mcp.sh lease       # reserva recurso/agente/desde (falla si ya existe: otro escritor activo)
./deploy_mcp.sh backup      # hash previo (pre.sha256) + copia de recuperación + estado previo
./deploy_mcp.sh canary      # arranca la versión nueva en el puerto 8791, smoke, la detiene; NO toca TARGET
./deploy_mcp.sh apply       # instala de forma atómica y reinicia
./deploy_mcp.sh readback    # hash instalado == esperado, servicio activo, smoke local y por proxy
```
Para comprobar también el proxy público añade `export PUBLIC_URL=https://mcp.hergertsynthora.com` antes de `readback`
(comprueba `POST /mcp`; no exige `/health` por el proxy).

**Qué debe verse en `readback`:** `READBACK OK`, y en `readback-local.json` `"ok": true` con estas comprobaciones:
`GET /health` 200, `active_products` = `UNKNOWN`, catálogo cargado (`tools>0`, sin `catalog_error`), rutas
desconocidas GET/POST 404, `initialize` válido, `initialize` sin params → `-32602`, notificación → 202, IDs esperados
presentes **tal cual** (guion y guion bajo), variante no publicada rechazada, ID desconocido → `unknown_tool` sin llamada externa.

## 3. Si algo falla: rollback (probado en el ensayo)
```bash
./deploy_mcp.sh rollback    # restaura la copia, reinicia, comprueba hash==pre.sha256, servicio activo y /health
```
Dispara rollback si: `readback` falla; el proxy `/mcp` deja de responder; `tools` baja a 0 con red disponible;
cualquier otro sector que comparta el host muestra errores nuevos.

## 4. Cierre
```bash
./deploy_mcp.sh release     # libera la reserva SOLO tras readback OK o rollback OK
```
Devuelve (sin secretos) `pre.sha256`, `canary-smoke.json`, `readback-local.json`, `readback-public.json` y la hora.

## 5. Qué NO está resuelto por este despliegue
- **Fuente del catálogo cambiada**: ahora se descubre desde `https://api.hergertsynthora.com/.well-known/x402.json`
  (antes `catalog.json`). Si el manifiesto vivo no tiene la forma esperada (`items[].accepts[]`), `/health` mostrará
  `tools: 0` / `catalog_error`: el smoke lo marca como fallo.
- **Endpoint = `resource` declarado** en el manifiesto (antes `API/v1/<nombre>`); no se ha comprobado que cada uno responda.
- **No es canal de venta**: `tools/call` no paga; un 402 se devuelve como error con el reto intacto.
- **`payTo`**: este servidor no lo decide ni lo cambia. La discrepancia entre manifiesto y cards sigue abierta
  (`PAYTO_RECONCILIATION_REQUIRED`).
- Estados: tras `readback OK` el MCP queda **desplegado y verificado técnicamente**; sigue sin haber venta, entrega pagada ni aceptación.
