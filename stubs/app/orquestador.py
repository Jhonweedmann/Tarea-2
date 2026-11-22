"""Stub del orquestador: responde siempre COMPLETADA sin llamar a nadie. Lo reemplaza B."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()
SAGAS: dict[str, dict] = {}
POR_CLAVE: dict[str, str] = {}


class MatriculaIn(BaseModel):
    estudiante_id: str
    curso_id: str


@router.post("/matriculas", status_code=201)
def crear(payload: MatriculaIn, request: Request, response: Response,
          idempotency_key: str | None = Header(default=None, max_length=128)):
    if idempotency_key and idempotency_key in POR_CLAVE:
        saga = SAGAS[POR_CLAVE[idempotency_key]]
        return JSONResponse(status_code=200, content=saga,
                            headers={"Location": f"/sagas/{saga['saga_id']}", "Idempotent-Replay": "true"})
    saga_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    saga = {
        "saga_id": saga_id, "estado": "COMPLETADA", "estudiante_id": payload.estudiante_id,
        "curso_id": payload.curso_id, "creada_en": ts, "actualizada_en": ts,
        "pasos": [{"paso": p, "resultado": "ok", "ts": ts} for p in ("reservar_cupo", "cobrar", "inscribir")],
        # El gateway envía X-Forwarded-Prefix (/api/v1) para que el enlace sea la ruta pública.
        "_links": {"self": {"href": f"{request.headers.get('x-forwarded-prefix', '')}/sagas/{saga_id}",
                            "method": "GET"}},
        "stub": True,
    }
    SAGAS[saga_id] = saga
    if idempotency_key:
        POR_CLAVE[idempotency_key] = saga_id
    response.headers["Location"] = f"/sagas/{saga_id}"  # el gateway lo traduce a la ruta pública
    return saga


@router.get("/sagas/{saga_id}")
def consultar(saga_id: str):
    if saga_id not in SAGAS:
        return JSONResponse(status_code=404, media_type="application/problem+json", content={
            "type": "https://httpstatuses.com/404", "title": "Saga no encontrada", "status": 404,
            "detail": "No existe una saga con ese id."})
    return SAGAS[saga_id]
