# Contratos (día 0)

Estos contratos fijan lo que cada servicio expone y lo que consume de los demás, para que cada
persona avance sin esperar a las otras. **Si alguien necesita cambiar un contrato, actualiza aquí
el archivo correspondiente y avisa a quien lo consume** (la columna "Consumidores").

| Servicio | Archivo | Dueño | Consumidores |
|---|---|---|---|
| Gateway (API pública) | [gateway.md](gateway.md) | A | clientes externos, experimentos de C |
| BFF web | [bff-web.md](bff-web.md) | A | gateway |
| Orquestador (Saga) | [orquestador.md](orquestador.md) | B | gateway |
| Inscripción | [inscripcion.md](inscripcion.md) | B | orquestador, BFF, gateway |
| Cupos (gRPC) | [cupos.md](cupos.md) | B | orquestador, BFF |
| Pagos | [pagos.md](pagos.md) | C | orquestador, BFF |

Mientras B y C construyen sus servicios, `docker compose` levanta **stubs** (`stubs/`) que
cumplen estos contratos con datos en memoria. Ver [docs/TRASPASO.md](../TRASPASO.md).

## Convenciones comunes

- Todos los servicios internos escuchan HTTP en el puerto **8000** (Cupos: gRPC en 50051).
  Solo el gateway publica un puerto al host (8080).
- `GET /health` → `200 {"status": "ok"}` en todos los servicios HTTP (lo usan los healthchecks).
- `GET /metrics` en todos los servicios internos (formato Prometheus). El gateway publica sus
  métricas en el puerto interno **9100**, no en el 8080.
- Errores en `application/problem+json` (RFC 9457): `type`, `title`, `status`, `detail`, y
  campos extra cuando aportan (por ejemplo `saga_id`).
- Los servicios internos **no autentican**: el gateway ya lo hizo. Confían en `X-Client-Id`.
- Los ids idempotentes (`reserva_id`, `cobro_id`, `inscripcion_id`) los genera el orquestador
  (es el `saga_id`). Repetir una operación con el mismo id devuelve el resultado guardado.
  Compensar algo que nunca se ejecutó es un no-op exitoso.
- Todos los servicios usan `estudiante_id` (no `alumno_id`) para referirse al estudiante.

## Cabeceras entre servicios

| Cabecera | Quién la pone | Qué hace el servicio que la recibe |
|---|---|---|
| `X-Correlation-Id` | El gateway (respeta la del cliente si es válida: `[A-Za-z0-9._:-]{1,128}`) | La incluye en cada log y la reenvía en cada llamada saliente. La devuelve en la respuesta. |
| `X-Client-Id` | El gateway, tras autenticar (descarta la que mande el cliente) | La incluye en los logs y la reenvía. |
| `Idempotency-Key` | El cliente, en `POST /api/v1/matriculas` | El gateway la reenvía sin tocarla al orquestador. |

En gRPC las mismas cabeceras viajan como metadata en minúscula (`x-correlation-id`, `x-client-id`).

El módulo [`comun/observabilidad.py`](../../comun/observabilidad.py) hace todo esto en una línea:

```python
from comun.observabilidad import instrumentar, obtener_logger, cabeceras_salientes, metadata_grpc

app = FastAPI(title="Orquestador")
instrumentar(app, "orquestador")      # logs JSON + X-Correlation-Id + métricas HTTP + GET /metrics
log = obtener_logger("orquestador")

log.info("cupo reservado", extra={"saga_id": saga_id, "paso": 1})
httpx.post(f"{PAGOS_URL}/cobros", json=cuerpo, headers=cabeceras_salientes(), timeout=2)
stub.ReservarCupo(req, timeout=2, metadata=metadata_grpc())
```

Para usarlo, el Dockerfile del servicio copia la carpeta: `COPY comun ./comun` (el contexto de
build es la raíz del repo) y agrega `prometheus-client==0.26.0` a su `requirements.txt`.

## Métricas

Nombres y etiquetas fijos: el dashboard de Grafana (`observabilidad/grafana/dashboards/forma-k.json`)
los consulta tal cual.

| Métrica | Tipo | Etiquetas | Quién la emite |
|---|---|---|---|
| `http_request_duration_seconds` | histograma | `service`, `route` | todos (vía `instrumentar`) |
| `http_requests_total` | contador | `service`, `route`, `status` | todos (vía `instrumentar`) |
| `gateway_rate_limited_total` | contador | `cliente` | gateway |
| `bff_degradaciones_total` | contador | `dependencia`, `motivo` | BFF |
| `saga_total` | contador | `resultado` (`COMPLETADA`, `COMPENSADA`, `ABORTADA_SIN_CUPO`, `ABORTADA_PAGOS_NO_DISPONIBLE`, ...) | orquestador (B) |
| `circuit_state` | gauge | `dep` (`pagos`) | orquestador / `resiliencia.py` (C). Valores: `0` cerrado, `1` half-open, `2` abierto |
| `bulkhead_in_flight` | gauge | `dep` | C |
| `bulkhead_rejected_total` | contador | `dep` | C |

`route` es la plantilla de la ruta (`/sagas/{saga_id}`), nunca la URL con el id, para no crear
una serie por cada id. En el gateway es el prefijo público (`/api/v1/matriculas`).
