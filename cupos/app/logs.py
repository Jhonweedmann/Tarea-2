"""Logs JSON de Cupos con el mismo formato que comun/observabilidad.py.

Cupos es un servidor gRPC (no FastAPI), así que no usa ese módulo: lee el correlation id de la
metadata gRPC que envía el llamador (x-correlation-id, x-client-id) y lo agrega a cada línea. Así
una misma saga se puede seguir en los logs de los cuatro servicios.
"""
import json
import logging
import sys
from datetime import datetime, timezone

_log = logging.getLogger("cupos")
if not _log.handlers:
    _manejador = logging.StreamHandler(sys.stdout)
    _manejador.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_manejador)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def registrar(context, mensaje: str, nivel: int = logging.INFO, **campos) -> None:
    metadata = dict(context.invocation_metadata()) if hasattr(context, "invocation_metadata") else {}
    datos = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "nivel": logging.getLevelName(nivel),
        "servicio": "cupos",
        "mensaje": mensaje,
        "correlation_id": metadata.get("x-correlation-id"),
    }
    if cliente := metadata.get("x-client-id"):
        datos["cliente_id"] = cliente
    datos.update(campos)
    _log.log(nivel, json.dumps(datos, ensure_ascii=False))
