"""Stub de Inscripción (en memoria, con est-001 precargado). Lo reemplaza B con inscripcion/."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()
ESTUDIANTES = {"est-001": {"id": "est-001", "nombre": "Ana Pérez", "email": "ana@example.com"}}
INSCRIPCIONES = {"insc-semilla-001": {"inscripcion_id": "insc-semilla-001", "estudiante_id": "est-001",
                                      "curso_id": "ARQ-101", "estado": "activa",
                                      "creada_en": "2026-11-22T12:00:00+00:00"}}


class EstudianteIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)


class InscripcionIn(BaseModel):
    inscripcion_id: str
    estudiante_id: str
    curso_id: str


def problem(code: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, media_type="application/problem+json", content={
        "type": f"https://httpstatuses.com/{code}", "title": title, "status": code, "detail": detail})


@router.post("/estudiantes", status_code=201)
def crear_estudiante(payload: EstudianteIn, response: Response):
    if any(e["email"] == payload.email for e in ESTUDIANTES.values()):
        return problem(409, "Correo duplicado", "Ya existe un estudiante con ese correo.")
    estudiante = {"id": str(uuid.uuid4()), "nombre": payload.nombre, "email": payload.email}
    ESTUDIANTES[estudiante["id"]] = estudiante
    response.headers["Location"] = f"/estudiantes/{estudiante['id']}"
    return estudiante


@router.get("/estudiantes")
def listar_estudiantes(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    todos = sorted(ESTUDIANTES.values(), key=lambda e: (e["nombre"], e["id"]))
    return {"items": todos[offset:offset + limit], "total": len(todos), "limit": limit, "offset": offset}


@router.get("/estudiantes/{estudiante_id}")
def consultar_estudiante(estudiante_id: str):
    if estudiante_id not in ESTUDIANTES:
        return problem(404, "Estudiante no encontrado", "No existe un estudiante con ese id.")
    return ESTUDIANTES[estudiante_id]


@router.post("/inscripciones", status_code=201)
def inscribir(payload: InscripcionIn):
    if (previa := INSCRIPCIONES.get(payload.inscripcion_id)) is not None:
        if previa["estado"] == "baja":
            return problem(409, "Inscripción dada de baja", "Ese inscripcion_id ya fue compensado.")
        return JSONResponse(status_code=200, content=previa)
    if payload.estudiante_id not in ESTUDIANTES:
        return problem(404, "Estudiante no encontrado", "No existe un estudiante con ese id.")
    inscripcion = {**payload.model_dump(), "estado": "activa", "creada_en": datetime.now(timezone.utc).isoformat()}
    INSCRIPCIONES[payload.inscripcion_id] = inscripcion
    return inscripcion


@router.post("/inscripciones/{inscripcion_id}/baja")
def dar_baja(inscripcion_id: str):
    # Si nunca existió, queda una "lápida" para rechazar una inscripción atrasada con ese id.
    inscripcion = INSCRIPCIONES.setdefault(inscripcion_id, {"inscripcion_id": inscripcion_id, "estado": "baja"})
    inscripcion["estado"] = "baja"
    return inscripcion


@router.get("/inscripciones")
def listar_inscripciones(estudiante_id: str):
    return {"items": [i for i in INSCRIPCIONES.values() if i.get("estudiante_id") == estudiante_id]}
