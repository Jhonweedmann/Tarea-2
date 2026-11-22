# Pagos (dueño: C)

Base interna: `http://pagos:8000`. Simula una pasarela de pago externa y frágil. No es pública.

> Hoy lo atiende el stub (`stubs/app/pagos.py`), con un cobro precargado para `est-001` y el
> mismo `/admin/falla`. C lo reemplaza con el servicio real (BD propia).

**`POST /cobros`** — paso 2 de la Saga, idempotente por `cobro_id` (= `saga_id`):

```json
{"cobro_id": "6f0c…", "estudiante_id": "est-001", "curso_id": "ARQ-101", "monto": 150000}
```

| Caso | Respuesta |
|---|---|
| Nuevo | `201` `{"cobro_id", "estudiante_id", "curso_id", "monto", "estado": "cobrado", "creado_en"}` |
| Mismo `cobro_id` repetido | `200` con lo guardado (**no cobra dos veces**) |
| `cobro_id` ya reembolsado | `409` |

`monto` es un entero en pesos. El orquestador lo toma de su configuración (`MATRICULA_MONTO`).

**`POST /cobros/{cobro_id}/reembolso`** — compensación, idempotente:

- Si existe y está cobrado → `"estado": "reembolsado"` → `200`.
- Si ya estaba reembolsado → `200` con lo mismo.
- Si **no existe** → `200` no-op, guardando una lápida para rechazar con `409` un cobro atrasado
  con ese id (timeout ambiguo: el orquestador no sabe si el cobro se aplicó).

**`GET /cobros?estudiante_id=est-001`** → `200` `{"items": [ ...cobros... ]}` (lo consume el BFF).

## Inyección de fallas

`POST /admin/falla` `{"latencia_ms": 5000, "caido": false, "tasa_error": 0.0}` → `200` con la
configuración vigente; `GET /admin/falla` la consulta. Mismo comportamiento que en Inscripción:
`caido` → `503`, `tasa_error` → `500` al azar, `latencia_ms` → espera antes de responder.
`/health`, `/metrics` y `/admin/*` nunca fallan (si no, Docker marcaría el contenedor como caído
y no se podría desactivar la falla).

Desde el host (Pagos no publica puerto):

```bash
docker compose exec pagos python -c "import httpx; print(httpx.post('http://localhost:8000/admin/falla', json={'latencia_ms': 5000}).json())"
```
