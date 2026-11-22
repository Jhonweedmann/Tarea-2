"""API Gateway: autentica, limita por cliente, asigna X-Correlation-Id y reenvía.

No tiene lógica de negocio (ADR-006): no conoce matrículas, cupos ni pagos, solo una tabla de
prefijos y a qué servicio interno corresponde cada uno.
"""
import hmac
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, start_http_server
from starlette.exceptions import HTTPException as StarletteHTTPException

from comun.observabilidad import cabeceras_salientes, cliente_id, instrumentar, obtener_logger

from .rate_limit import LimitadorVentanaFija


@dataclass(frozen=True)
class Ruta:
    prefijo: str          # ruta pública
    servicio: str
    destino: str          # URL base del servicio interno
    prefijo_interno: str  # ruta en el servicio interno que reemplaza al prefijo público
    timeout: float


def _url(variable: str, defecto: str) -> str:
    return os.getenv(variable, defecto).rstrip("/")


ORQUESTADOR = _url("ORQUESTADOR_URL", "http://orquestador:8000")
INSCRIPCION = _url("INSCRIPCION_URL", "http://inscripcion:8000")
BFF = _url("BFF_URL", "http://bff-web:8000")
TIMEOUT = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "5"))
# La saga hace 3 llamadas con timeouts y reintentos propios: el gateway debe esperar más que eso.
TIMEOUT_SAGA = float(os.getenv("GATEWAY_TIMEOUT_SAGA_SECONDS", "15"))

PREFIJO_PUBLICO = "/api/v1"
# Orden: del prefijo más específico al más general.
RUTAS = (
    Ruta("/api/v1/matriculas", "orquestador", ORQUESTADOR, "/matriculas", TIMEOUT_SAGA),
    Ruta("/api/v1/sagas", "orquestador", ORQUESTADOR, "/sagas", TIMEOUT),
    Ruta("/api/v1/estudiantes", "inscripcion", INSCRIPCION, "/estudiantes", TIMEOUT),
    Ruta("/api/v1/cursos", "bff-web", BFF, "/web/cursos", TIMEOUT),
    Ruta("/api/v1/web", "bff-web", BFF, "/web", TIMEOUT),
)

# token -> cliente. Se puede reemplazar entero con la variable GATEWAY_CLIENTES (mismo formato JSON).
CLIENTES_DEFECTO = {
    "desarrollo-seguro": {"cliente": "secretaria", "rol": "escritura", "limite": 20},
    "solo-lectura": {"cliente": "consulta", "rol": "lectura", "limite": 20},
    "portal-web": {"cliente": "portal-web", "rol": "escritura", "limite": 20},
    "carga-experimentos": {"cliente": "experimentos", "rol": "escritura", "limite": 100000},
}
CLIENTES = json.loads(os.environ["GATEWAY_CLIENTES"]) if os.getenv("GATEWAY_CLIENTES") else CLIENTES_DEFECTO
VENTANA_S = int(os.getenv("RATE_LIMIT_VENTANA_SECONDS", "10"))
METRICAS_PUERTO = int(os.getenv("METRICAS_PUERTO", "9100"))

METODOS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
METODOS_LECTURA = {"GET", "HEAD", "OPTIONS"}
# Cabeceras que no se reenvían al servicio interno: las hop-by-hop (RFC 9110 §7.6.1), las que
# httpx recalcula, el token (los servicios internos no autentican) y las que pone el gateway
# (un cliente no puede hacerse pasar por otro mandando su propio X-Client-Id).
EXCLUIDAS_SOLICITUD = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length", "authorization",
    "x-client-id", "x-correlation-id", "x-forwarded-for", "x-forwarded-prefix",
}
# httpx ya descomprimió el cuerpo, así que content-encoding/length del servicio ya no aplican.
EXCLUIDAS_RESPUESTA = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length",
                       "x-correlation-id"}

RATE_LIMITED = Counter("gateway_rate_limited_total", "Solicitudes rechazadas con 429, por cliente", ["cliente"])
log = obtener_logger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
    app.state.redis = aioredis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"), socket_timeout=0.5, socket_connect_timeout=0.5,
    )
    app.state.limitador = LimitadorVentanaFija(app.state.redis, VENTANA_S)
    if METRICAS_PUERTO:
        # Puerto interno aparte: el 8080 es público y no debe exponer métricas.
        start_http_server(METRICAS_PUERTO)
    yield
    await app.state.http.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="API Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def problem(code: int, detail: str, title: str | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=code, media_type="application/problem+json", headers=headers,
        content={"type": f"https://httpstatuses.com/{code}", "title": title or HTTPStatus(code).phrase,
                 "status": code, "detail": detail},
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc: StarletteHTTPException):
    return problem(exc.status_code, str(exc.detail), headers=exc.headers)


def buscar_ruta(path: str) -> Ruta | None:
    for ruta in RUTAS:
        if path == ruta.prefijo or path.startswith(ruta.prefijo + "/"):
            return ruta
    return None


def location_publica(location: str, origen: Ruta) -> str:
    """Traduce un Location interno (/sagas/1) a la ruta pública (/api/v1/sagas/1), como `proxy_redirect` en Nginx."""
    for ruta in RUTAS:
        if ruta.destino == origen.destino and (location == ruta.prefijo_interno
                                               or location.startswith(ruta.prefijo_interno + "/")):
            return ruta.prefijo + location[len(ruta.prefijo_interno):]
    return location


def autenticar(request: Request) -> tuple[dict | None, JSONResponse | None]:
    esquema, _, token = request.headers.get("authorization", "").partition(" ")
    if esquema.lower() != "bearer" or not token:
        return None, problem(401, "Se requiere el header Authorization: Bearer <token>.", "No autenticado",
                             {"WWW-Authenticate": "Bearer"})
    # Se recorre la tabla completa comparando en tiempo constante, como en la Tarea 1 (ADR-005).
    encontrado = None
    for conocido, cliente in CLIENTES.items():
        if hmac.compare_digest(token.encode(), conocido.encode()):
            encontrado = cliente
    if encontrado is None:
        return None, problem(401, "El token no es válido.", "No autenticado",
                             {"WWW-Authenticate": 'Bearer error="invalid_token"'})
    if encontrado["rol"] == "lectura" and request.method not in METODOS_LECTURA:
        return None, problem(403, "El token es de solo lectura y no permite modificar recursos.", "Sin permiso")
    return encontrado, None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.api_route("/api/{resto:path}", methods=METODOS)
async def reenviar(request: Request, resto: str):
    path = request.url.path
    ruta = buscar_ruta(path)
    request.state.ruta_metrica = ruta.prefijo if ruta else "sin_ruta"

    cliente, error = autenticar(request)
    if error:
        return error
    request.state.cliente_id = cliente["cliente"]
    cliente_id.set(cliente["cliente"])

    limite = await request.app.state.limitador.consumir(cliente["cliente"], int(cliente["limite"]))
    cabeceras_limite = {}
    if limite is not None:
        cabeceras_limite = {"X-RateLimit-Limit": str(limite.limite), "X-RateLimit-Remaining": str(limite.restantes)}
        if not limite.permitido:
            RATE_LIMITED.labels(cliente["cliente"]).inc()
            return problem(429, f"Se superó el límite de {limite.limite} solicitudes cada {VENTANA_S} s para "
                                f"este cliente.", "Demasiadas solicitudes",
                           {"Retry-After": str(limite.reintentar_en), **cabeceras_limite})

    if ruta is None:
        return problem(404, "No existe una ruta pública con ese prefijo.", "Recurso no encontrado")
    # "." o ".." (también codificados como %2e) permitirían salir del prefijo en el servicio interno,
    # por ejemplo /api/v1/estudiantes/../admin/falla. Se rechazan antes de reenviar.
    if any(segmento in {".", ".."} for segmento in path.split("/")):
        return problem(400, "La ruta no puede contener segmentos '.' ni '..'.", "Solicitud inválida")

    crudo = request.scope.get("raw_path", path.encode()).decode("latin-1")
    destino = ruta.destino + ruta.prefijo_interno + crudo[len(ruta.prefijo):]
    if request.url.query:
        destino += "?" + request.url.query
    cabeceras = [(k, v) for k, v in request.headers.items() if k.lower() not in EXCLUIDAS_SOLICITUD]
    cabeceras += list(cabeceras_salientes().items())
    if request.client:
        cabeceras.append(("X-Forwarded-For", request.client.host))
    # Para que el servicio arme `_links` con la ruta pública (p. ej. /api/v1/sagas/{id}).
    cabeceras.append(("X-Forwarded-Prefix", PREFIJO_PUBLICO))

    try:
        respuesta = await request.app.state.http.request(
            request.method, destino, headers=cabeceras, content=await request.body(),
            timeout=httpx.Timeout(ruta.timeout, connect=2.0),
        )
    except httpx.TimeoutException:
        log.warning("timeout del servicio interno", extra={"servicio": ruta.servicio, "timeout_s": ruta.timeout})
        return problem(504, f"El servicio {ruta.servicio} no respondió dentro de {ruta.timeout:g} s.",
                       "Tiempo de espera agotado", cabeceras_limite)
    except httpx.TransportError as exc:
        log.warning("servicio interno inalcanzable", extra={"servicio": ruta.servicio, "error": repr(exc)})
        return problem(502, f"No se pudo conectar con el servicio {ruta.servicio}.", "Servicio no disponible",
                       cabeceras_limite)

    salida = {k: v for k, v in respuesta.headers.items() if k.lower() not in EXCLUIDAS_RESPUESTA}
    if "location" in respuesta.headers:
        salida = {k: v for k, v in salida.items() if k.lower() != "location"}
        salida["Location"] = location_publica(respuesta.headers["location"], ruta)
    salida.update(cabeceras_limite)
    return Response(content=respuesta.content, status_code=respuesta.status_code, headers=salida)


# Logs JSON, X-Correlation-Id y métricas HTTP (también de las respuestas 401/429/404). Las métricas
# se publican en METRICAS_PUERTO, no en /metrics del puerto público.
instrumentar(app, "gateway", exponer_metricas=False)
