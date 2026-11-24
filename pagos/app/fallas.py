"""POST/GET /admin/falla: inyección de latencia, caída o errores al azar (misma lógica que stubs/app/fallas.py).

Es lo que convierte a este servicio en la "pasarela externa frágil" del escenario 3.
"""
import asyncio
import random

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Nunca fallan: si /health fallara, Docker marcaría el contenedor como caído; si /admin fallara,
# no habría forma de quitar la falla.
EXENTAS = ("/health", "/metrics", "/admin/")


class Falla(BaseModel):
    latencia_ms: int | None = Field(default=None, ge=0, le=60000)
    caido: bool | None = None
    tasa_error: float | None = Field(default=None, ge=0, le=1)


def instalar_fallas(app: FastAPI) -> None:
    estado = {"latencia_ms": 0, "caido": False, "tasa_error": 0.0}

    @app.middleware("http")
    async def inyectar(request: Request, call_next):
        if request.url.path.startswith(EXENTAS):
            return await call_next(request)
        if estado["latencia_ms"]:
            await asyncio.sleep(estado["latencia_ms"] / 1000)
        if estado["caido"] or random.random() < estado["tasa_error"]:
            codigo = 503 if estado["caido"] else 500
            return JSONResponse(status_code=codigo, media_type="application/problem+json", content={
                "type": f"https://httpstatuses.com/{codigo}", "title": "Falla inyectada", "status": codigo,
                "detail": "Falla configurada con /admin/falla."})
        return await call_next(request)

    @app.post("/admin/falla")
    def configurar(falla: Falla):
        estado.update(falla.model_dump(exclude_none=True))
        return estado

    @app.get("/admin/falla")
    def consultar():
        return estado
