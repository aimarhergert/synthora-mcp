#!/usr/bin/env bash
# deploy_mcp.sh — despliegue controlado de synthora_mcp.py. Lo ejecuta el PROPIETARIO en el host.
# Un subcomando = un cambio. Ninguno paga, firma ni usa secretos.
#
#   plan      muestra lo que se hara y NO cambia nada
#   lease     toma la reserva recurso/agente/desde (falla si ya existe)
#   backup    hash previo + copia de recuperacion + estado previo del servicio
#   canary    arranca NEW_FILE en CANARY_PORT, pasa el smoke y lo detiene (no toca TARGET)
#   apply     instala NEW_FILE en TARGET (atomico) y reinicia el servicio
#   readback  hash instalado == esperado, servicio activo, smoke local y (opcional) por proxy
#   rollback  restaura la copia, reinicia y verifica el hash previo
#   release   libera la reserva
#
# Variables: TARGET NEW_FILE EXPECTED_SHA256 AGENT   (obligatorias segun subcomando)
#            SERVICE=synthora-mcp.service PORT=8790 CANARY_PORT=8791 PUBLIC_URL= WORKDIR=/var/tmp/synthora-mcp-deploy
#            SYSTEMCTL=systemctl PYTHON=python3 SMOKE_EXTRA="--allow-empty-catalog" (solo ensayos sin red)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${SERVICE:-synthora-mcp.service}"
PORT="${PORT:-8790}"
CANARY_PORT="${CANARY_PORT:-8791}"
WORKDIR="${WORKDIR:-/var/tmp/synthora-mcp-deploy}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PYTHON="${PYTHON:-python3}"
SMOKE="${SMOKE:-$HERE/smoke_mcp.py}"
SMOKE_EXTRA="${SMOKE_EXTRA:-}"
EXPECT_IDS="${EXPECT_IDS:-}"
LEASE="$WORKDIR/lease.json"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
die() { echo "ERROR: $*" >&2; exit 1; }
need() { for v in "$@"; do [ -n "${!v:-}" ] || die "falta la variable $v"; done; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
smoke() { # $1=base  resto=args extra
  local base="$1"; shift
  "$PYTHON" "$SMOKE" --base "$base" ${EXPECT_IDS:+--expect-ids "$EXPECT_IDS"} "$@"
}
holds_lease() { [ -f "$LEASE" ] && grep -q "\"agent\": \"$AGENT\"" "$LEASE"; }

cmd="${1:-plan}"
mkdir -p "$WORKDIR"
case "$cmd" in
  plan)
    need TARGET NEW_FILE EXPECTED_SHA256 AGENT
    echo "TARGET=$TARGET (sha256 actual: $( [ -f "$TARGET" ] && sha "$TARGET" || echo ausente ))"
    echo "NEW_FILE=$NEW_FILE (sha256: $( [ -f "$NEW_FILE" ] && sha "$NEW_FILE" || echo ausente ), esperado: $EXPECTED_SHA256)"
    echo "SERVICE=$SERVICE PORT=$PORT CANARY_PORT=$CANARY_PORT WORKDIR=$WORKDIR"
    echo "Orden: lease -> backup -> canary -> apply -> readback -> (rollback si falla) -> release"
    ;;
  lease)
    need AGENT TARGET
    ( set -o noclobber
      printf '{"resource": "synthora-mcp:%s", "agent": "%s", "since": "%s"}\n' "$TARGET" "$AGENT" "$(now)" > "$LEASE" ) \
      2>/dev/null || die "reserva ya tomada: $(cat "$LEASE")"
    echo "reserva tomada: $(cat "$LEASE")"
    ;;
  backup)
    need AGENT TARGET
    holds_lease || die "no tienes la reserva (ejecuta lease)"
    [ -f "$TARGET" ] || die "TARGET no existe"
    sha "$TARGET" > "$WORKDIR/pre.sha256"
    cp -p "$TARGET" "$WORKDIR/synthora_mcp.py.bak"
    [ "$(sha "$WORKDIR/synthora_mcp.py.bak")" = "$(cat "$WORKDIR/pre.sha256")" ] || die "la copia no coincide con el original"
    "$SYSTEMCTL" is-active "$SERVICE" > "$WORKDIR/pre.service_state" 2>&1 || true
    echo "hash previo: $(cat "$WORKDIR/pre.sha256")"
    echo "copia: $WORKDIR/synthora_mcp.py.bak  estado previo del servicio: $(cat "$WORKDIR/pre.service_state")"
    ;;
  canary)
    need NEW_FILE EXPECTED_SHA256
    [ "$(sha "$NEW_FILE")" = "$EXPECTED_SHA256" ] || die "NEW_FILE no coincide con EXPECTED_SHA256"
    SYNTHORA_MCP_HOST=127.0.0.1 SYNTHORA_MCP_PORT="$CANARY_PORT" "$PYTHON" "$NEW_FILE" > "$WORKDIR/canary.log" 2>&1 &
    pid=$!
    trap 'kill "$pid" 2>/dev/null || true' EXIT
    for _ in $(seq 1 40); do curl -fsS "http://127.0.0.1:$CANARY_PORT/health" >/dev/null 2>&1 && break; sleep 0.25; done
    # shellcheck disable=SC2086
    smoke "http://127.0.0.1:$CANARY_PORT" $SMOKE_EXTRA > "$WORKDIR/canary-smoke.json" || { cat "$WORKDIR/canary-smoke.json"; die "canary FALLA; no se aplica nada"; }
    echo "canary OK (ver $WORKDIR/canary-smoke.json)"
    ;;
  apply)
    need AGENT TARGET NEW_FILE EXPECTED_SHA256
    holds_lease || die "no tienes la reserva"
    [ -f "$WORKDIR/pre.sha256" ] || die "falta backup previo"
    [ "$(sha "$NEW_FILE")" = "$EXPECTED_SHA256" ] || die "NEW_FILE no coincide con EXPECTED_SHA256"
    tmp="$(dirname "$TARGET")/.synthora_mcp.new.$$"
    cp "$NEW_FILE" "$tmp" && chmod --reference="$TARGET" "$tmp" && mv -f "$tmp" "$TARGET"
    "$SYSTEMCTL" restart "$SERVICE"
    echo "instalado y reiniciado a las $(now). Siguiente paso: readback"
    ;;
  readback)
    need TARGET EXPECTED_SHA256
    got="$(sha "$TARGET")"
    [ "$got" = "$EXPECTED_SHA256" ] || die "READBACK FALLA: hash instalado $got != esperado"
    [ "$("$SYSTEMCTL" is-active "$SERVICE")" = "active" ] || die "READBACK FALLA: servicio no activo"
    for _ in $(seq 1 40); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 0.25; done
    # shellcheck disable=SC2086
    smoke "http://127.0.0.1:$PORT" $SMOKE_EXTRA > "$WORKDIR/readback-local.json" || { cat "$WORKDIR/readback-local.json"; die "READBACK FALLA: smoke local (considera rollback)"; }
    if [ -n "${PUBLIC_URL:-}" ]; then
      smoke "$PUBLIC_URL" --no-health $SMOKE_EXTRA > "$WORKDIR/readback-public.json" || { cat "$WORKDIR/readback-public.json"; die "READBACK FALLA: proxy /mcp (considera rollback)"; }
    fi
    echo "READBACK OK $(now): hash=$got servicio=active smoke_local=ok smoke_publico=${PUBLIC_URL:+ok}${PUBLIC_URL:-no_comprobado}"
    ;;
  rollback)
    need AGENT TARGET
    holds_lease || die "no tienes la reserva"
    [ -f "$WORKDIR/synthora_mcp.py.bak" ] && [ -f "$WORKDIR/pre.sha256" ] || die "no hay copia de recuperacion"
    tmp="$(dirname "$TARGET")/.synthora_mcp.rb.$$"
    cp -p "$WORKDIR/synthora_mcp.py.bak" "$tmp" && mv -f "$tmp" "$TARGET"
    "$SYSTEMCTL" restart "$SERVICE"
    [ "$(sha "$TARGET")" = "$(cat "$WORKDIR/pre.sha256")" ] || die "ROLLBACK FALLA: hash distinto del previo"
    for _ in $(seq 1 40); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 0.25; done
    [ "$("$SYSTEMCTL" is-active "$SERVICE")" = "active" ] || die "ROLLBACK FALLA: servicio no activo"
    curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null || die "ROLLBACK FALLA: /health no responde"
    echo "ROLLBACK OK $(now): hash previo restaurado, servicio activo, /health responde"
    ;;
  release)
    need AGENT
    holds_lease || die "no tienes la reserva"
    rm -f "$LEASE"; echo "reserva liberada $(now)"
    ;;
  *) die "subcomando desconocido: $cmd" ;;
esac
