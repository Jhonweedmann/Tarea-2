"""Orquestador de la Saga de matrícula (servicio interno).

Público a través del gateway en /api/v1/matriculas y /api/v1/sagas. Contrato:
docs/contratos/orquestador.md.
"""
import os
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from comun.observabilidad import instrumentar, obtener_logger

from .database import RepositorioSagas, connect

DB_PATH = os.getenv("ORQUESTADOR_DB", "/data/orquestador.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = connect(DB_PATH)
    app.state.sagas = RepositorioSagas(db)
    yield
    db.close()


app = FastAPI(title="Orquestador de la Saga de matrícula", version="1.0.0", lifespan=lifespan)
# Logs JSON con X-Correlation-Id, métricas HTTP y GET /metrics.
instrumentar(app, "orquestador")
log = obtener_logger("orquestador")


# ---------------------------------------------------------------- Errores (problem+json)

def problem(code: int, title: str, detail: str, headers: dict | None = None, **extensiones) -> JSONResponse:
    return JSONResponse(
        status_code=code, media_type="application/problem+json", headers=headers,
        content={"type": f"https://httpstatuses.com/{code}", "title": title, "status": code, "detail": detail,
                 **extensiones},
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc: StarletteHTTPException):
    return problem(exc.status_code, HTTPStatus(exc.status_code).phrase, str(exc.detail), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc: RequestValidationError):
    errores = [{"loc": list(error["loc"]), "message": error["msg"]} for error in exc.errors()]
    return problem(422, "Solicitud inválida", "El cuerpo o sus parámetros no cumplen el contrato.", errors=errores)


@app.exception_handler(Exception)
async def error_inesperado(_, exc: Exception):
    log.exception("error inesperado")
    return problem(500, "Error interno", "Ocurrió un error inesperado al procesar la solicitud.")


# ---------------------------------------------------------------- Representación

def enlace_saga(request: Request, saga_id: str) -> str:
    # El gateway envía X-Forwarded-Prefix (/api/v1) para que el cliente reciba la ruta pública.
    return f"{request.headers.get('x-forwarded-prefix', '')}/sagas/{saga_id}"


def representacion(request: Request, saga_id: str) -> dict | None:
    repo: RepositorioSagas = request.app.state.sagas
    saga = repo.obtener(saga_id)
    if saga is None:
        return None
    resultado = {campo: saga[campo] for campo in
                 ("saga_id", "estado", "estudiante_id", "curso_id", "creada_en", "actualizada_en")}
    if saga["error"]:
        resultado["error"] = saga["error"]
    resultado["pasos"] = [{clave: valor for clave, valor in paso.items() if valor is not None}
                          for paso in repo.pasos(saga_id)]
    resultado["_links"] = {"self": {"href": enlace_saga(request, saga_id), "method": "GET"}}
    return resultado


# ---------------------------------------------------------------- Rutas

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/sagas/{saga_id}")
def consultar_saga(saga_id: str, request: Request):
    saga = representacion(request, saga_id)
    if saga is None:
        return problem(404, "Saga no encontrada", "No existe una saga con ese id.")
    return saga
