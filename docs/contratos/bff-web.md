# BFF web (dueño: A)

Base interna: `http://bff-web:8000`. Público a través del gateway en `/api/v1/web/...` y `/api/v1/cursos`.

## `GET /web/alumnos/{estudiante_id}/panel`

Junta en una sola respuesta lo que la pantalla del alumno necesita. Hace **en paralelo** cuatro
llamadas, cada una con su propio timeout (`BFF_TIMEOUT_SECONDS`, 2 s por defecto):

| Sección | Fuente | Si falla |
|---|---|---|
| `estudiante` | Inscripción `GET /estudiantes/{id}` | `404` si no existe; `503`/`504` si Inscripción no responde (sin estudiante no hay panel) |
| `inscripciones` | Inscripción `GET /inscripciones?estudiante_id=` | sección `{"disponible": false}` |
| `cursos` | Cupos `ListarCursos` (gRPC) | sección `{"disponible": false}` |
| `pagos` | Pagos `GET /cobros?estudiante_id=` | sección `{"disponible": false}` |

```json
{
  "estudiante": {"id": "est-001", "nombre": "Ana Pérez", "email": "ana@example.com"},
  "inscripciones": {"disponible": true, "items": [
    {"inscripcion_id": "…", "estudiante_id": "est-001", "curso_id": "ARQ-101", "estado": "activa",
     "creada_en": "…", "curso_nombre": "Arquitectura de Sistemas"}
  ]},
  "cursos": {"disponible": true, "items": [
    {"id": "ARQ-101", "nombre": "Arquitectura de Sistemas", "cupos_totales": 2, "cupos_disponibles": 1}
  ]},
  "pagos": {"disponible": false, "motivo": "timeout", "mensaje": "Pagos no está disponible; los cobros se mostrarán cuando se recupere."},
  "degradado": true
}
```

- `degradado: true` si alguna sección quedó no disponible. El código sigue siendo `200`.
- `motivo`: `timeout`, `sin_conexion` o `error_<status>` (p. ej. `error_503`).
- `curso_nombre` solo aparece si la sección `cursos` respondió.
- Cada degradación suma 1 a `bff_degradaciones_total{dependencia, motivo}`.

## `GET /web/cursos` y `GET /web/cursos/{curso_id}`

Catálogo de cursos con cupos, leído de Cupos por gRPC (`ListarCursos` / `ConsultarCurso`).

```json
{"items": [{"id": "ARQ-101", "nombre": "Arquitectura de Sistemas", "cupos_totales": 2, "cupos_disponibles": 2}]}
```

`GET /web/cursos/{id}` devuelve un solo objeto, `404` si no existe y `503`/`504` si Cupos no responde.
