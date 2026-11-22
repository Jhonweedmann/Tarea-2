"""Arranca el stub elegido con STUB_SERVICIO (orquestador, inscripcion o pagos)."""
import importlib
import os

from fastapi import FastAPI

from comun.observabilidad import instrumentar

from .fallas import instalar_fallas

SERVICIO = os.getenv("STUB_SERVICIO", "pagos")
if SERVICIO not in {"orquestador", "inscripcion", "pagos"}:
    raise RuntimeError(f"STUB_SERVICIO desconocido: {SERVICIO}")

app = FastAPI(title=f"Stub de {SERVICIO}")
app.include_router(importlib.import_module(f".{SERVICIO}", __package__).router)
instalar_fallas(app)
# Después de las fallas, para que la latencia inyectada también quede en las métricas.
instrumentar(app, SERVICIO)


@app.get("/health")
def health():
    return {"status": "ok", "stub": True}
