"""Servicio de Pagos (interno): simula la pasarela de pago externa y frágil. Contrato: docs/contratos/pagos.md.

- Base de datos propia (SQLite en /data/pagos.db).
- POST /cobros es idempotente por cobro_id (= saga_id): repetirlo NO cobra dos veces.
- POST /cobros/{id}/reembolso es idempotente y, si el cobro nunca existió, deja una lápida.
- /admin/falla inyecta latencia, caída o errores al azar para los experimentos de resiliencia.
"""
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http import HTTPStatus

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from comun.observabilidad import instrumentar, obtener_logger

from .database import connect, seed
from .fallas import instalar_fallas

DB_PATH = os.getenv("PAGOS_DB", "/data/pagos.db")
# Cobro de ejemplo de est-001 (el que traía el stub). Desactivado por defecto: es un cobro sin
# inscripción ni cupo reservado, justo el "cobro sin operación" que la Saga debe evitar, y se vería
# en el panel del BFF al partir. PAGOS_SEMILLA=1 lo vuelve a cargar si se quiere.
SEMILLA = os.getenv("PAGOS_SEMILLA", "0") == "1"

OPERACIONES = Counter("pagos_operaciones_total", "Operaciones de Pagos, por tipo de resultado", ["operacion"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = connect(DB_PATH)
    if SEMILLA:
        seed(app.state.db)
    # Los endpoints sincronos corren en varios hilos: este lock hace atomico "leer si existe y luego
    # escribir", así dos reintentos simultaneos del mismo cobro_id no pueden cobrar dos veces.
    app.state.lock = threading.Lock()
    yield
    app.state.db.close()


app = FastAPI(title="Pagos (pasarela simulada)", version="1.0.0", lifespan=lifespan)
instalar_fallas(app)
# Después de las fallas, para que la latencia y los errores inyectados también queden en las métricas.
instrumentar(app, "pagos")
log = obtener_logger("pagos")


class CobroIn(BaseModel):
    cobro_id: str = Field(min_length=1, max_length=128)
    estudiante_id: str = Field(min_length=1, max_length=128)
    curso_id: str = Field(min_length=1, max_length=64)
    monto: int = Field(gt=0)


def problem(code: int, title: str, detail: str, **extensiones) -> JSONResponse:
    return JSONResponse(status_code=code, media_type="application/problem+json", content={
        "type": f"https://httpstatuses.com/{code}", "title": title, "status": code, "detail": detail,
        **extensiones})


@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc: StarletteHTTPException):
    return problem(exc.status_code, HTTPStatus(exc.status_code).phrase, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc: RequestValidationError):
    errores = [{"loc": list(error["loc"]), "message": error["msg"]} for error in exc.errors()]
    return problem(422, "Solicitud inválida", "El cuerpo o sus parámetros no cumplen el contrato.", errors=errores)


@app.exception_handler(Exception)
async def error_inesperado(_, exc: Exception):
    log.exception("error inesperado")
    return problem(500, "Error interno", "Ocurrió un error inesperado al procesar la solicitud.")


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def buscar(db, cobro_id: str):
    return db.execute("SELECT * FROM cobros WHERE cobro_id=?", (cobro_id,)).fetchone()


def representacion(row) -> dict:
    # Una lapida no tiene estudiante, curso ni monto: esos campos se omiten en vez de enviarlos en null.
    campos = ("cobro_id", "estudiante_id", "curso_id", "monto", "estado", "creado_en")
    return {campo: row[campo] for campo in campos if row[campo] is not None}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/cobros", status_code=status.HTTP_201_CREATED)
def cobrar(payload: CobroIn, request: Request, response: Response):
    db = request.app.state.db
    with request.app.state.lock:
        previo = buscar(db, payload.cobro_id)
        if previo is not None:
            if previo["estado"] == "reembolsado":
                # Lapida o compensacion ya hecha: llegó un cobro atrasado (timeout ambiguo).
                OPERACIONES.labels("cobro_rechazado_reembolsado").inc()
                log.warning("cobro atrasado rechazado: ya estaba reembolsado",
                            extra={"saga_id": payload.cobro_id, "paso": "cobrar"})
                return problem(409, "Cobro reembolsado", "Ese cobro_id ya fue compensado.")
            if (previo["estudiante_id"], previo["curso_id"], previo["monto"]) != (
                    payload.estudiante_id, payload.curso_id, payload.monto):
                return problem(409, "cobro_id en uso", "Ese cobro_id ya se usó con otros datos.")
            # Repetición de la misma llamada: se devuelve lo guardado con 200, SIN cobrar otra vez.
            OPERACIONES.labels("cobro_repetido").inc()
            log.info("cobro repetido: no se cobra de nuevo", extra={"saga_id": payload.cobro_id, "paso": "cobrar"})
            return JSONResponse(status_code=200, content=representacion(previo))
        marca = ahora()
        db.execute("INSERT INTO cobros VALUES (?, ?, ?, ?, 'cobrado', ?, ?)",
                   (payload.cobro_id, payload.estudiante_id, payload.curso_id, payload.monto, marca, marca))
        db.commit()
        OPERACIONES.labels("cobro_nuevo").inc()
        log.info("cobro realizado", extra={"saga_id": payload.cobro_id, "paso": "cobrar",
                                           "estudiante_id": payload.estudiante_id, "monto": payload.monto})
        response.headers["Location"] = f"/cobros/{payload.cobro_id}"
        return representacion(buscar(db, payload.cobro_id))


@app.post("/cobros/{cobro_id}/reembolso")
def reembolsar(cobro_id: str, request: Request):
    db = request.app.state.db
    with request.app.state.lock:
        previo = buscar(db, cobro_id)
        if previo is None:
            # Nunca existió (o aún no llega): no-op exitoso, pero queda una lápida.
            marca = ahora()
            db.execute("INSERT INTO cobros VALUES (?, NULL, NULL, NULL, 'reembolsado', ?, ?)", (cobro_id, marca, marca))
            db.commit()
            OPERACIONES.labels("reembolso_lapida").inc()
            log.info("reembolso sin cobro previo: lápida", extra={"saga_id": cobro_id, "paso": "reembolsar"})
        elif previo["estado"] == "cobrado":
            db.execute("UPDATE cobros SET estado='reembolsado', actualizado_en=? WHERE cobro_id=?", (ahora(), cobro_id))
            db.commit()
            OPERACIONES.labels("reembolso").inc()
            log.info("cobro reembolsado", extra={"saga_id": cobro_id, "paso": "reembolsar"})
        else:
            OPERACIONES.labels("reembolso_repetido").inc()  # ya estaba reembolsado: idempotente
        return representacion(buscar(db, cobro_id))


@app.get("/cobros")
def listar(request: Request, estudiante_id: str = Query(min_length=1)):
    filas = request.app.state.db.execute(
        "SELECT * FROM cobros WHERE estudiante_id=? ORDER BY creado_en, cobro_id", (estudiante_id,))
    return {"items": [representacion(fila) for fila in filas]}
