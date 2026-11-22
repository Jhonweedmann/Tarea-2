"""Observabilidad común: métricas Prometheus, logs JSON y propagación de X-Correlation-Id.

Uso en un servicio FastAPI (ver docs/contratos/README.md):

    from comun.observabilidad import instrumentar, obtener_logger, cabeceras_salientes

    app = FastAPI(...)
    instrumentar(app, "orquestador")          # middleware + GET /metrics + logs JSON
    log = obtener_logger("orquestador")
    log.info("paso completado", extra={"saga_id": saga_id, "paso": "cupo"})
    httpx.post(url, json=..., headers=cabeceras_salientes())   # propaga correlation y cliente

Todos los servicios usan los mismos nombres de métricas para que el dashboard de Grafana los
agrupe por la etiqueta `service`.
"""
import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

CABECERA_CORRELACION = "X-Correlation-Id"
CABECERA_CLIENTE = "X-Client-Id"
# Se acepta el id que llega solo si es corto y de caracteres seguros; si no, se genera uno nuevo.
# Evita que un cliente inyecte saltos de línea o textos enormes en los logs.
_CORRELACION_VALIDA = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Rutas que no se miden ni se registran: los healthchecks de Docker y el scrape de Prometheus
# llegan cada pocos segundos y solo meterían ruido.
RUTAS_SILENCIOSAS = {"/health", "/metrics"}

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
cliente_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("cliente_id", default=None)

HTTP_DURACION = Histogram(
    "http_request_duration_seconds", "Duración de las solicitudes HTTP atendidas por el servicio",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30),
)
HTTP_TOTAL = Counter(
    "http_requests_total", "Solicitudes HTTP atendidas, por código de estado", ["service", "route", "status"],
)


def correlacion_valida(valor: str | None) -> str:
    if valor and _CORRELACION_VALIDA.match(valor):
        return valor
    return str(uuid.uuid4())


def cabeceras_salientes() -> dict[str, str]:
    """Cabeceras que se agregan a toda llamada HTTP hacia otro servicio interno."""
    cabeceras = {}
    if (cid := correlation_id.get()) is not None:
        cabeceras[CABECERA_CORRELACION] = cid
    if (cliente := cliente_id.get()) is not None:
        cabeceras[CABECERA_CLIENTE] = cliente
    return cabeceras


def metadata_grpc() -> list[tuple[str, str]]:
    """Lo mismo que cabeceras_salientes(), en el formato de metadata de gRPC (claves en minúscula)."""
    return [(clave.lower(), valor) for clave, valor in cabeceras_salientes().items()]


class FormatoJson(logging.Formatter):
    """Una línea JSON por evento. Los campos pasados con `extra=` (p. ej. saga_id) se agregan tal cual."""
    _ESTANDAR = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

    def __init__(self, servicio: str):
        super().__init__()
        self.servicio = servicio

    def format(self, record: logging.LogRecord) -> str:
        datos = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "nivel": record.levelname,
            "servicio": self.servicio,
            "mensaje": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        if (cliente := cliente_id.get()) is not None:
            datos["cliente_id"] = cliente
        datos.update({k: v for k, v in vars(record).items() if k not in self._ESTANDAR})
        if record.exc_info:
            datos["error"] = self.formatException(record.exc_info)
        return json.dumps(datos, ensure_ascii=False, default=str)


def configurar_logs(servicio: str) -> None:
    raiz = logging.getLogger()
    if any(isinstance(h.formatter, FormatoJson) for h in raiz.handlers):
        return
    manejador = logging.StreamHandler(sys.stdout)
    manejador.setFormatter(FormatoJson(servicio))
    raiz.addHandler(manejador)
    raiz.setLevel(os.getenv("LOG_NIVEL", "INFO").upper())
    # httpx registra cada llamada en INFO; el log "solicitud" de cada servicio ya cubre eso.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def obtener_logger(servicio: str) -> logging.Logger:
    return logging.getLogger(servicio)


def _ruta_metrica(request: Request) -> str:
    # Un servicio puede fijar su propia etiqueta (el gateway usa el prefijo público de la ruta).
    if etiqueta := getattr(request.state, "ruta_metrica", None):
        return etiqueta
    # Se usa la plantilla de la ruta (/sagas/{saga_id}) y no la URL real para no crear una serie por id.
    ruta = request.scope.get("route")
    return getattr(ruta, "path", None) or "sin_ruta"


def instrumentar(app: FastAPI, servicio: str, exponer_metricas: bool = True) -> None:
    """Agrega logs JSON, X-Correlation-Id, métricas HTTP y (opcionalmente) GET /metrics a la app.

    exponer_metricas=False sirve para el gateway, que publica las métricas en un puerto interno
    aparte para no exponerlas en el puerto público.
    """
    configurar_logs(servicio)
    log = obtener_logger(servicio)

    @app.middleware("http")
    async def observar(request: Request, call_next):
        cid = correlacion_valida(request.headers.get(CABECERA_CORRELACION))
        token_cid = correlation_id.set(cid)
        token_cliente = cliente_id.set(request.headers.get(CABECERA_CLIENTE))
        inicio = time.perf_counter()
        estado = 500
        try:
            respuesta = await call_next(request)
            estado = respuesta.status_code
            respuesta.headers[CABECERA_CORRELACION] = cid
            return respuesta
        finally:
            if request.url.path not in RUTAS_SILENCIOSAS:
                duracion = time.perf_counter() - inicio
                ruta = _ruta_metrica(request)
                HTTP_DURACION.labels(servicio, ruta).observe(duracion)
                HTTP_TOTAL.labels(servicio, ruta, str(estado)).inc()
                extra = {"metodo": request.method, "ruta": ruta, "path": request.url.path, "status": estado,
                         "duracion_ms": round(duracion * 1000, 1)}
                # El gateway identifica al cliente dentro del endpoint; lo deja en request.state para este log.
                if cliente := getattr(request.state, "cliente_id", None):
                    extra["cliente_id"] = cliente
                log.info("solicitud", extra=extra)
            correlation_id.reset(token_cid)
            cliente_id.reset(token_cliente)

    if exponer_metricas:
        @app.get("/metrics", include_in_schema=False)
        def metricas():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
