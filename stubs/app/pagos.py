"""Stub de Pagos (en memoria, con un cobro precargado para est-001). Lo reemplaza C con pagos/."""
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()
COBROS = {"insc-semilla-001": {"cobro_id": "insc-semilla-001", "estudiante_id": "est-001", "curso_id": "ARQ-101",
                               "monto": 150000, "estado": "cobrado", "creado_en": "2026-11-22T12:00:00+00:00"}}


class CobroIn(BaseModel):
    cobro_id: str
    estudiante_id: str
    curso_id: str
    monto: int = Field(gt=0)


@router.post("/cobros", status_code=201)
def cobrar(payload: CobroIn):
    if (previo := COBROS.get(payload.cobro_id)) is not None:
        if previo["estado"] == "reembolsado":
            return JSONResponse(status_code=409, media_type="application/problem+json", content={
                "type": "https://httpstatuses.com/409", "title": "Cobro reembolsado", "status": 409,
                "detail": "Ese cobro_id ya fue compensado."})
        return JSONResponse(status_code=200, content=previo)
    cobro = {**payload.model_dump(), "estado": "cobrado", "creado_en": datetime.now(timezone.utc).isoformat()}
    COBROS[payload.cobro_id] = cobro
    return cobro


@router.post("/cobros/{cobro_id}/reembolso")
def reembolsar(cobro_id: str):
    # Si nunca existió, queda una "lápida" para rechazar un cobro atrasado con ese id.
    cobro = COBROS.setdefault(cobro_id, {"cobro_id": cobro_id, "estado": "reembolsado"})
    cobro["estado"] = "reembolsado"
    return cobro


@router.get("/cobros")
def listar(estudiante_id: str):
    return {"items": [c for c in COBROS.values() if c.get("estudiante_id") == estudiante_id]}
