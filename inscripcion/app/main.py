"""Servicio de Inscripción (interno).

Viene de refactorizar el servicio de Matrículas de la Tarea 1. Ya no llama a Cupos ni autentica:
la autenticación la hace el gateway y la coordinación con Cupos y Pagos la hace el orquestador
(Saga). Aquí quedan solo los datos propios de este servicio: estudiantes e inscripciones.

Las rutas van sin /v1: el gateway traduce /api/v1/estudiantes -> /estudiantes.
"""
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import yaml
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, EmailStr, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .database import connect

DB_PATH = os.getenv("INSCRIPCION_DB", "/data/inscripcion.db")
CONTRATO = Path(__file__).resolve().parents[1] / "openapi.yaml"
TITULOS = {
    400: "Solicitud inválida", 404: "Recurso no encontrado", 405: "Método no permitido", 409: "Conflicto",
    422: "Solicitud inválida", 500: "Error interno",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = connect(DB_PATH)
    yield
    app.state.db.close()


app = FastAPI(title="Servicio de Inscripción", version="2.0.0", lifespan=lifespan)

# Contract-first: el servicio publica el openapi.yaml escrito a mano, no uno generado desde el código.
_contrato = yaml.safe_load(CONTRATO.read_text(encoding="utf-8"))
app.openapi = lambda: _contrato


class EstudianteIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    email: EmailStr


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
