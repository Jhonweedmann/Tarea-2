# Inscripción (dueño: B)

Base interna: `http://inscripcion:8000`. Sale de refactorizar `matriculas/`: sin gRPC y sin auth
(lo hace el gateway). Público solo `/estudiantes` (vía gateway en `/api/v1/estudiantes`); el resto
lo usan el orquestador y el BFF.

> Implementación: `inscripcion/` (contrato OpenAPI en `inscripcion/openapi.yaml`), con el
> estudiante `est-001` precargado. El stub (`stubs/app/inscripcion.py`) queda solo como referencia.

## Estudiantes

| Operación | Respuesta |
|---|---|
| `POST /estudiantes` `{"nombre": "Ana Pérez", "email": "ana@example.com"}` | `201` `{"id", "nombre", "email"}` + `Location`; `409` si el correo existe; `422` si el cuerpo es inválido |
| `GET /estudiantes/{id}` | `200` `{"id", "nombre", "email"}`; `404` |
| `GET /estudiantes?limit=&offset=` | `200` `{"items": [...], "total", "limit", "offset"}` (igual que en la Tarea 1) |

## Inscripciones (pasos 3 y compensación de la Saga)

**`POST /inscripciones`** — idempotente por `inscripcion_id` (= `saga_id`):

```json
{"inscripcion_id": "6f0c…", "estudiante_id": "est-001", "curso_id": "ARQ-101"}
```

| Caso | Respuesta |
|---|---|
| Nueva | `201` `{"inscripcion_id", "estudiante_id", "curso_id", "estado": "activa", "creada_en"}` + `Location: /inscripciones/{id}` |
| Mismo `inscripcion_id` repetido | `200` con lo guardado (no crea otra) |
| Estudiante no existe | `404` |
| El estudiante ya tiene otra inscripción activa en ese curso | `409` |
| `inscripcion_id` ya dado de baja (la compensación llegó antes) | `409` |
| Mismo `inscripcion_id` con otro estudiante o curso | `409` |

**`POST /inscripciones/{inscripcion_id}/baja`** — compensación, idempotente:

- Si existe y está activa → la marca `"estado": "baja"` → `200`.
- Si ya estaba de baja → `200` con lo mismo.
- Si **no existe** → `200` no-op. Guarda una "lápida" (`estado: baja`, sin `estudiante_id` ni
  `curso_id`) para que un `POST /inscripciones` atrasado con ese id (timeout ambiguo) se rechace con `409`.

**`GET /inscripciones/{inscripcion_id}`** → `200` con la inscripción (o la lápida); `404` si no existe.
Sirve para revisar el estado final tras una Saga.

**`GET /inscripciones?estudiante_id=est-001`** → `200` `{"items": [ ...inscripciones... ]}`
(lo consume el BFF; los campos son los del `201` de arriba).

## Inyección de fallas

`POST /admin/falla` `{"latencia_ms": 0, "caido": false, "tasa_error": 0.0}` → `200` con la
configuración vigente. `GET /admin/falla` la consulta. Con `caido: true` todo lo que no sea
`/health`, `/metrics` ni `/admin/*` responde `503`; `tasa_error` (0–1) responde `500` al azar;
`latencia_ms` agrega una espera antes de responder. Sirve para hacer fallar el paso 3 (escenario 2).
