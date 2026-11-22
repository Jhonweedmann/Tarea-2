# Orquestador — Saga de matrícula (dueño: B)

Base interna: `http://orquestador:8000`. Público a través del gateway en `/api/v1/matriculas` y `/api/v1/sagas`.

> Hoy lo atiende el stub (`stubs/app/orquestador.py`), que responde siempre `COMPLETADA` sin
> llamar a nadie. B lo reemplaza por la implementación real manteniendo esta forma.

## `POST /matriculas`

Cabecera opcional `Idempotency-Key` (≤ 128 caracteres). Cuerpo:

```json
{"estudiante_id": "est-001", "curso_id": "ARQ-101"}
```

Ejecuta los 3 pasos (ver `docs/PLAN.md` §2): reservar cupo → cobrar → inscribir, usando el
`saga_id` como clave idempotente en cada uno.

| Resultado | Código | Cuerpo |
|---|---|---|
| Saga completada | `201` + `Location: /sagas/{saga_id}` | representación de la saga |
| Repetición con la misma `Idempotency-Key` | `200` + `Idempotent-Replay: true` | la misma representación guardada |
| Misma `Idempotency-Key` aún en proceso | `409` | problem+json |
| Estudiante o curso no existe | `404` | problem+json con `saga_id` |
| Sin cupo (falla el paso 1, no se cobra) | `409` | problem+json con `saga_id`, estado `ABORTADA_SIN_CUPO` |
| Pagos no disponible / circuito abierto | `503` + `Retry-After` | problem+json con `saga_id`, estado `ABORTADA_PAGOS_NO_DISPONIBLE` |
| Falla un paso posterior y se compensa | `502` (o `409` si es de negocio) | problem+json con `saga_id`, estado `COMPENSADA` |

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
`COMPENSACION_PENDIENTE`, `ABORTADA_SIN_CUPO`, `ABORTADA_PAGOS_NO_DISPONIBLE`.
Los pasos de compensación se agregan a `pasos` con nombres `reembolsar`, `liberar_cupo`, `dar_baja`.

## `GET /sagas/{saga_id}`

`200` con la representación de arriba, `404` si no existe.

## Métrica

`saga_total{resultado}`: +1 cada vez que una saga llega a un estado final.
