"""Servicio de Inscripción (interno).

Viene de refactorizar el servicio de Matrículas de la Tarea 1. Ya no llama a Cupos ni autentica:
la autenticación la hace el gateway y la coordinación con Cupos y Pagos la hace el orquestador
(Saga). Aquí quedan solo los datos propios de este servicio: estudiantes e inscripciones.

Las rutas van sin /v1: el gateway traduce /api/v1/estudiantes -> /estudiantes.
"""
import os
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

import yaml
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, EmailStr, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from comun.observabilidad import instrumentar, obtener_logger

from .database import connect
from .fallas import instalar_fallas

DB_PATH = os.getenv("INSCRIPCION_DB", "/data/inscripcion.db")
CONTRATO = Path(__file__).resolve().parents[1] / "openapi.yaml"
TITULOS = {
    400: "Solicitud inválida", 404: "Recurso no encontrado", 405: "Método no permitido", 409: "Conflicto",
    422: "Solicitud inválida", 500: "Error interno",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = connect(DB_PATH)
    # FastAPI atiende los endpoints sincronos en varios hilos. Este lock hace que "leer si ya
    # existe y luego escribir" sea atomico en inscribir y dar de baja (sin carreras entre reintentos).
    app.state.lock = threading.Lock()
    yield
    app.state.db.close()


app = FastAPI(title="Servicio de Inscripción", version="2.0.0", lifespan=lifespan)
# /admin/falla permite hacer fallar el paso 3 de la Saga (escenario 2).
instalar_fallas(app)
# Despues de las fallas, para que los errores y la latencia inyectados tambien queden en las
# metricas. Agrega logs JSON con X-Correlation-Id, metricas HTTP y GET /metrics.
instrumentar(app, "inscripcion")
log = obtener_logger("inscripcion")

# Contract-first: el servicio publica el openapi.yaml escrito a mano, no uno generado desde el código.
_contrato = yaml.safe_load(CONTRATO.read_text(encoding="utf-8"))
app.openapi = lambda: _contrato


class EstudianteIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    email: EmailStr


class InscripcionIn(BaseModel):
    inscripcion_id: str = Field(min_length=1, max_length=128)
    estudiante_id: str
    curso_id: str


class Problema(Exception):
    def __init__(self, code: int, title: str, detail: str):
        self.code, self.title, self.detail = code, title, detail


def problem(code: int, title: str, detail: str, headers: dict | None = None, **extensiones):
    return JSONResponse(
        status_code=code,
        media_type="application/problem+json",
        headers=headers,
        content={"type": f"https://httpstatuses.com/{code}", "title": title, "status": code, "detail": detail,
                 **extensiones},
    )


def titulo(code: int) -> str:
    return TITULOS.get(code) or HTTPStatus(code).phrase


@app.exception_handler(Problema)
async def problema_handler(_, exc: Problema):
    return problem(exc.code, exc.title, exc.detail)


@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc: StarletteHTTPException):
    return problem(exc.status_code, titulo(exc.status_code), str(exc.detail), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc: RequestValidationError):
    errores = [{"loc": list(error["loc"]), "message": error["msg"]} for error in exc.errors()]
    return problem(422, "Solicitud inválida", "El cuerpo o sus parámetros no cumplen el contrato.", errors=errores)


@app.exception_handler(Exception)
async def error_inesperado(_, exc: Exception):
    return problem(500, "Error interno", "Ocurrió un error inesperado al procesar la solicitud.")


def pagina(db, tabla: str, orden: str, limit: int, offset: int) -> dict:
    total = db.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
    filas = db.execute(f"SELECT * FROM {tabla} ORDER BY {orden} LIMIT ? OFFSET ?", (limit, offset))
    return {"items": [dict(fila) for fila in filas], "total": total, "limit": limit, "offset": offset}


@app.get("/health", tags=["operacion"])
def health():
    return {"status": "ok"}


@app.get("/openapi.yaml", include_in_schema=False)
def contrato_yaml():
    return PlainTextResponse(CONTRATO.read_text(encoding="utf-8"), media_type="application/yaml")


# ---------------------------------------------------------------- Estudiantes

@app.post("/estudiantes", status_code=status.HTTP_201_CREATED, tags=["estudiantes"])
def crear_estudiante(payload: EstudianteIn, request: Request, response: Response):
    estudiante = {"id": str(uuid.uuid4()), "nombre": payload.nombre, "email": str(payload.email)}
    try:
        request.app.state.db.execute("INSERT INTO estudiantes VALUES (:id, :nombre, :email)", estudiante)
        request.app.state.db.commit()
    except sqlite3.IntegrityError:
        raise Problema(409, "Correo duplicado", "Ya existe un estudiante con ese correo.")
    # Ruta interna: el gateway la traduce a /api/v1/estudiantes/{id}.
    response.headers["Location"] = f"/estudiantes/{estudiante['id']}"
    return estudiante


@app.get("/estudiantes", tags=["estudiantes"])
def listar_estudiantes(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return pagina(request.app.state.db, "estudiantes", "nombre, id", limit, offset)


@app.get("/estudiantes/{estudiante_id}", tags=["estudiantes"])
def consultar_estudiante(estudiante_id: str, request: Request):
    row = request.app.state.db.execute("SELECT * FROM estudiantes WHERE id=?", (estudiante_id,)).fetchone()
    if not row:
        raise Problema(404, "Estudiante no encontrado", "No existe un estudiante con ese id.")
    return dict(row)


# ---------------------------------------------------------------- Inscripciones (Saga)
# POST /inscripciones es el paso 3 de la Saga y POST /inscripciones/{id}/baja su compensacion.
# Las dos son idempotentes por inscripcion_id (= saga_id): repetirlas no crea ni cambia nada.

CAMPOS_INSCRIPCION = ("inscripcion_id", "estudiante_id", "curso_id", "estado", "creada_en")


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def buscar_inscripcion(db, inscripcion_id: str):
    return db.execute("SELECT * FROM inscripciones WHERE inscripcion_id=?", (inscripcion_id,)).fetchone()


def representacion_inscripcion(row) -> dict:
    # Una lapida no tiene estudiante ni curso: esos campos se omiten en vez de enviarlos en null.
    return {campo: row[campo] for campo in CAMPOS_INSCRIPCION if row[campo] is not None}


@app.post("/inscripciones", status_code=status.HTTP_201_CREATED, tags=["inscripciones"])
def inscribir(payload: InscripcionIn, request: Request, response: Response):
    db = request.app.state.db
    with request.app.state.lock:
        previa = buscar_inscripcion(db, payload.inscripcion_id)
        if previa is not None:
            if previa["estado"] == "baja":
                # Lapida o compensacion ya hecha: llego una inscripcion atrasada (timeout ambiguo).
                raise Problema(409, "Inscripción dada de baja",
                               "Ese inscripcion_id ya fue compensado y no se puede volver a inscribir.")
            if (previa["estudiante_id"], previa["curso_id"]) != (payload.estudiante_id, payload.curso_id):
                raise Problema(409, "inscripcion_id en uso", "Ese inscripcion_id ya se usó con otro estudiante o curso.")
            # Repeticion de la misma llamada: se devuelve lo guardado con 200, sin crear otra.
            log.info("inscripcion repetida", extra={"saga_id": payload.inscripcion_id, "paso": "inscribir"})
            return JSONResponse(status_code=200, content=representacion_inscripcion(previa))

        if not db.execute("SELECT 1 FROM estudiantes WHERE id=?", (payload.estudiante_id,)).fetchone():
            raise Problema(404, "Estudiante no encontrado", "No existe un estudiante con ese id.")
        marca = ahora()
        try:
            db.execute("INSERT INTO inscripciones VALUES (?, ?, ?, 'activa', ?, ?)",
                       (payload.inscripcion_id, payload.estudiante_id, payload.curso_id, marca, marca))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            raise Problema(409, "Inscripción duplicada",
                           "El estudiante ya tiene una inscripción activa en este curso.")
        log.info("inscripcion creada", extra={"saga_id": payload.inscripcion_id, "paso": "inscribir",
                                              "estudiante_id": payload.estudiante_id, "curso_id": payload.curso_id})
        response.headers["Location"] = f"/inscripciones/{payload.inscripcion_id}"
        return representacion_inscripcion(buscar_inscripcion(db, payload.inscripcion_id))


@app.post("/inscripciones/{inscripcion_id}/baja", tags=["inscripciones"])
def dar_baja(inscripcion_id: str, request: Request):
    db = request.app.state.db
    with request.app.state.lock:
        previa = buscar_inscripcion(db, inscripcion_id)
        if previa is None:
            # Nunca se inscribio (o la inscripcion aun no llega): no-op exitoso, pero queda una
            # lapida para que un POST /inscripciones atrasado con este id se rechace con 409.
            marca = ahora()
            db.execute("INSERT INTO inscripciones VALUES (?, NULL, NULL, 'baja', ?, ?)",
                       (inscripcion_id, marca, marca))
            db.commit()
            log.info("baja sin inscripcion previa: lapida", extra={"saga_id": inscripcion_id, "paso": "dar_baja"})
        elif previa["estado"] == "activa":
            db.execute("UPDATE inscripciones SET estado='baja', actualizada_en=? WHERE inscripcion_id=?",
                       (ahora(), inscripcion_id))
            db.commit()
            log.info("inscripcion dada de baja", extra={"saga_id": inscripcion_id, "paso": "dar_baja"})
        # Si ya estaba de baja no se hace nada: la compensacion es idempotente.
        return representacion_inscripcion(buscar_inscripcion(db, inscripcion_id))


@app.get("/inscripciones", tags=["inscripciones"])
def listar_inscripciones(request: Request, estudiante_id: str = Query(min_length=1)):
    filas = request.app.state.db.execute(
        "SELECT * FROM inscripciones WHERE estudiante_id=? ORDER BY creada_en, inscripcion_id", (estudiante_id,))
    return {"items": [representacion_inscripcion(fila) for fila in filas]}


@app.get("/inscripciones/{inscripcion_id}", tags=["inscripciones"])
def consultar_inscripcion(inscripcion_id: str, request: Request):
    row = buscar_inscripcion(request.app.state.db, inscripcion_id)
    if row is None:
        raise Problema(404, "Inscripción no encontrada", "No existe una inscripción con ese id.")
    return representacion_inscripcion(row)
