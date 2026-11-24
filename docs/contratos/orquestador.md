# Orquestador — Saga de matrícula (dueño: B)

Base interna: `http://orquestador:8000`. Público a través del gateway en `/api/v1/matriculas` y `/api/v1/sagas`.

> Implementación: `orquestador/app/` (la Saga está en `saga.py`). El stub
> (`stubs/app/orquestador.py`) queda solo como referencia.

## `POST /matriculas`

Cabecera opcional `Idempotency-Key` (≤ 128 caracteres). Cuerpo:

```json
{"estudiante_id": "est-001", "curso_id": "ARQ-101"}
```

Ejecuta los 3 pasos (ver `docs/PLAN.md` §2): reservar cupo → cobrar → inscribir, usando el
`saga_id` como clave idempotente en cada uno. Antes valida que el estudiante exista
(`validar_estudiante`); si Inscripción no responde a esa validación, la saga sigue y el paso 3
vuelve a validar. El monto lo fija el orquestador (`MATRICULA_MONTO`, por defecto 150000).

| Resultado | Código | Cuerpo |
|---|---|---|
| Saga completada | `201` + `Location: /sagas/{saga_id}` | representación de la saga |
| Repetición con la misma `Idempotency-Key` | `200` + `Idempotent-Replay: true` | la misma representación guardada |
| Misma `Idempotency-Key` aún en proceso | `409` | problem+json con el `saga_id` que la tiene |
| Misma `Idempotency-Key` con otro estudiante o curso | `422` | problem+json |
| Se envió `Idempotency-Key` y Redis no responde | `503` + `Retry-After` | problem+json (no se crea la saga) |
| Estudiante o curso no existe | `404` | problem+json con `saga_id`, estado `ABORTADA_NO_ENCONTRADO` |
| Sin cupo (falla el paso 1, no se cobra) | `409` | problem+json con `saga_id`, estado `ABORTADA_SIN_CUPO` |
| Cupos no responde (se libera la reserva por si acaso) | `503` + `Retry-After` | problem+json con `saga_id`, estado `COMPENSADA` |
| Pagos no disponible / circuito abierto | `503` + `Retry-After` | problem+json con `saga_id`, estado `ABORTADA_PAGOS_NO_DISPONIBLE` |
| Falla un paso posterior y se compensa | `502` (o `409`/`404` si es de negocio) | problem+json con `saga_id`, estado `COMPENSADA` |
| Una compensación falla | el código del paso que falló | problem+json con `saga_id`, estado `COMPENSACION_PENDIENTE` (el worker la termina) |

La clave se guarda en Redis (base 1) por 24 h y apunta al `saga_id`. Solo una saga `COMPLETADA`
conserva su clave: si la saga falla, la clave se suelta para que el cliente pueda reintentar con
ella (la saga nueva usa otro `saga_id`, así que no puede cobrar dos veces lo mismo).

Los problem+json de la saga incluyen `saga_id`, `estado` y `_links.self` para consultar el historial.

Representación de la saga (también la de `GET /sagas/{id}`):

```json
{
  "saga_id": "6f0c…",
  "estado": "COMPLETADA",
  "estudiante_id": "est-001",
  "curso_id": "ARQ-101",
  "creada_en": "2026-11-22T16:00:00+00:00",
  "actualizada_en": "2026-11-22T16:00:01+00:00",
  "pasos": [
    {"paso": "validar_estudiante", "resultado": "ok", "ts": "…"},
    {"paso": "reservar_cupo", "resultado": "ok", "ts": "…"},
    {"paso": "cobrar", "resultado": "ok", "ts": "…"},
    {"paso": "inscribir", "resultado": "ok", "ts": "…"}
  ],
  "_links": {"self": {"href": "/sagas/6f0c…", "method": "GET"}}
}
```

`Location` y `_links`: el servicio usa su ruta interna en `Location` (el gateway la traduce) y
antepone `X-Forwarded-Prefix` (si viene) en los `href` de `_links`, para que el cliente reciba
`/api/v1/sagas/…`.

Estados: `INICIADA`, `CUPO_RESERVADO`, `PAGO_COBRADO`, `COMPLETADA`, `COMPENSANDO`, `COMPENSADA`,
`COMPENSACION_PENDIENTE`, `ABORTADA_SIN_CUPO`, `ABORTADA_PAGOS_NO_DISPONIBLE`, `ABORTADA_NO_ENCONTRADO`.
Los pasos de compensación se agregan a `pasos` con nombres `dar_baja`, `reembolsar`, `liberar_cupo`
(siempre en ese orden, el inverso de los pasos).

`resultado` de cada paso: `ok` (se aplicó), `error` (seguro que no se aplicó; no se compensa) o
`incierto` (timeout o conexión cortada: pudo aplicarse, así que se compensa por si acaso). Si hay
`detalle`, explica la falla. Si la saga falló, la representación trae además `error`.

## `GET /sagas/{saga_id}`

`200` con la representación de arriba, `404` si no existe.

## Métrica

`saga_total{resultado}`: +1 cada vez que una saga llega a un estado final.
