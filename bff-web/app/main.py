"""BFF web: arma en una sola respuesta lo que necesita la pantalla del alumno.

Consulta Inscripción, Cupos (gRPC) y Pagos en paralelo, cada uno con su propio tiempo máximo. Si
una fuente secundaria falla, su sección se marca como no disponible y el resto del panel se
entrega igual (degradación parcial). Contrato: docs/contratos/bff-web.md.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from http import HTTPStatus

import grpc
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from comun.observabilidad import cabeceras_salientes, instrumentar, metadata_grpc, obtener_logger

from . import cupos_pb2, cupos_pb2_grpc

INSCRIPCION = os.getenv("INSCRIPCION_URL", "http://inscripcion:8000").rstrip("/")
PAGOS = os.getenv("PAGOS_URL", "http://pagos:8000").rstrip("/")
CUPOS_TARGET = os.getenv("CUPOS_GRPC_TARGET", "cupos:50051")
# Tiempo máximo por llamada. Las cuatro van en paralelo, así que el panel tarda como mucho esto.
TIMEOUT = float(os.getenv("BFF_TIMEOUT_SECONDS", "2"))

DEGRADACIONES = Counter("bff_degradaciones_total", "Secciones del panel entregadas como no disponibles",
                        ["dependencia", "motivo"])
MENSAJES = {
    "inscripciones": "Las inscripciones no están disponibles en este momento.",
    "cursos": "El catálogo de cursos no está disponible en este momento.",
    "pagos": "Pagos no está disponible; los cobros se mostrarán cuando se recupere.",
}
log = obtener_logger("bff-web")


class Degradado(Exception):
    """La dependencia no respondió bien. `motivo` va a la respuesta y a la métrica."""
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


class NoEncontrado(Exception):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient()
    app.state.canal = grpc.aio.insecure_channel(CUPOS_TARGET)
    app.state.cupos = cupos_pb2_grpc.CuposStub(app.state.canal)
    yield
    await app.state.http.aclose()
    await app.state.canal.close()


app = FastAPI(title="BFF web", lifespan=lifespan)
instrumentar(app, "bff-web")


def problem(code: int, detail: str, title: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=code, media_type="application/problem+json", content={
        "type": f"https://httpstatuses.com/{code}", "title": title or HTTPStatus(code).phrase,
        "status": code, "detail": detail})


async def con_limite(coro):
    """Aplica el tiempo máximo total a una llamada y traduce los errores de red a Degradado."""
    try:
        return await asyncio.wait_for(coro, TIMEOUT)
    except (TimeoutError, httpx.TimeoutException):
        raise Degradado("timeout")
    except httpx.TransportError:
        raise Degradado("sin_conexion")
    except grpc.RpcError as error:
        codigo = error.code()
        if codigo == grpc.StatusCode.NOT_FOUND:
            raise NoEncontrado()
        if codigo == grpc.StatusCode.DEADLINE_EXCEEDED:
            raise Degradado("timeout")
        if codigo == grpc.StatusCode.UNAVAILABLE:
            raise Degradado("sin_conexion")
        raise Degradado(f"error_{codigo.name.lower()}")


async def get_json(request: Request, url: str, params: dict | None = None) -> dict:
    respuesta = await request.app.state.http.get(url, params=params, headers=cabeceras_salientes())
    if respuesta.status_code == 404:
        raise NoEncontrado()
    if respuesta.status_code != 200:
        raise Degradado(f"error_{respuesta.status_code}")
    return respuesta.json()


def curso_dict(curso) -> dict:
    return {"id": curso.id, "nombre": curso.nombre, "cupos_totales": curso.cupos_totales,
            "cupos_disponibles": curso.cupos_disponibles}


async def listar_cursos(request: Request) -> list[dict]:
    llamada = request.app.state.cupos.ListarCursos(cupos_pb2.ListarCursosRequest(), timeout=TIMEOUT,
                                                   metadata=metadata_grpc())
    return [curso_dict(curso) async for curso in llamada]


async def consultar_curso(request: Request, curso_id: str) -> dict:
    curso = await request.app.state.cupos.ConsultarCurso(cupos_pb2.ConsultarCursoRequest(curso_id=curso_id),
                                                         timeout=TIMEOUT, metadata=metadata_grpc())
    return curso_dict(curso)


def seccion(dependencia: str, resultado) -> dict:
    if isinstance(resultado, BaseException):
        motivo = resultado.motivo if isinstance(resultado, Degradado) else "respuesta_invalida"
        if not isinstance(resultado, Degradado):
            log.error("respuesta inesperada de una dependencia", exc_info=resultado,
                      extra={"dependencia": dependencia})
        DEGRADACIONES.labels(dependencia, motivo).inc()
        log.warning("sección degradada", extra={"dependencia": dependencia, "motivo": motivo})
        return {"disponible": False, "motivo": motivo, "mensaje": MENSAJES[dependencia]}
    return {"disponible": True, "items": resultado}


def problema_dependencia(servicio: str, error: Degradado) -> JSONResponse:
    if error.motivo == "timeout":
        return problem(504, f"{servicio} no respondió dentro de {TIMEOUT:g} s.", "Tiempo de espera agotado")
    return problem(503, f"{servicio} no está disponible ({error.motivo}).", "Servicio no disponible")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/web/alumnos/{estudiante_id}/panel")
async def panel(estudiante_id: str, request: Request):
    async def inscripciones():
        return (await get_json(request, f"{INSCRIPCION}/inscripciones", {"estudiante_id": estudiante_id}))["items"]

    async def cobros():
        return (await get_json(request, f"{PAGOS}/cobros", {"estudiante_id": estudiante_id}))["items"]

    estudiante, insc, cursos, pagos = await asyncio.gather(
        con_limite(get_json(request, f"{INSCRIPCION}/estudiantes/{estudiante_id}")),
        con_limite(inscripciones()),
        con_limite(listar_cursos(request)),
        con_limite(cobros()),
        return_exceptions=True,
    )
    # Sin estudiante no hay panel: esa es la única fuente obligatoria.
    if isinstance(estudiante, NoEncontrado):
        return problem(404, "No existe un estudiante con ese id.", "Estudiante no encontrado")
    if isinstance(estudiante, Degradado):
        return problema_dependencia("Inscripción", estudiante)
    if isinstance(estudiante, BaseException):
        raise estudiante

    respuesta = {
        "estudiante": estudiante,
        "inscripciones": seccion("inscripciones", insc),
        "cursos": seccion("cursos", cursos),
        "pagos": seccion("pagos", pagos),
    }
    if respuesta["cursos"]["disponible"] and respuesta["inscripciones"]["disponible"]:
        nombres = {curso["id"]: curso["nombre"] for curso in cursos}
        for item in respuesta["inscripciones"]["items"]:
            if item.get("curso_id") in nombres:
                item["curso_nombre"] = nombres[item["curso_id"]]
    respuesta["degradado"] = not all(respuesta[s]["disponible"] for s in ("inscripciones", "cursos", "pagos"))
    return respuesta


@app.get("/web/cursos")
async def cursos(request: Request):
    try:
        return {"items": await con_limite(listar_cursos(request))}
    except Degradado as error:
        return problema_dependencia("Cupos", error)


@app.get("/web/cursos/{curso_id}")
async def curso(curso_id: str, request: Request):
    try:
        return await con_limite(consultar_curso(request, curso_id))
    except NoEncontrado:
        return problem(404, "No existe un curso con ese id.", "Curso no encontrado")
    except Degradado as error:
        return problema_dependencia("Cupos", error)
