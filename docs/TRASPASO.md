# Traspaso entre fases

## Fase A (Persona A) — terminada

Rama: `feat/gateway-bff`. Todo lo de la Persona A en [PLAN.md](PLAN.md) §4 está hecho, más el
trabajo del día 0 que A necesitaba para avanzar (contratos y esqueleto del compose).

| Qué | Dónde | Cómo se verificó |
|---|---|---|
| Contratos de todos los servicios, cabeceras y métricas | `docs/contratos/` | — (acordar con B y C) |
| Observabilidad común (logs JSON, correlation id, métricas) | `comun/observabilidad.py` | usada por gateway, BFF y stubs |
| Gateway: rutas, auth por cliente, rate limit Redis, correlation id | `gateway/` | `tests/test_gateway.py` + en vivo |
| BFF: panel con degradación parcial, catálogo de cursos | `bff-web/` | `tests/test_bff.py` + en vivo con Pagos detenido, lento y en 503 |
| Stubs de orquestador, inscripción y pagos (con `/admin/falla`) | `stubs/` | en vivo |
| Compose: solo 8080 (y 3000) publicados, healthchecks, Prometheus, Grafana | `docker-compose.yml`, `observabilidad/` | `docker compose up`: todo healthy, 5 targets `up`, dashboard cargado |
| ADR-006, README, `request.http` | `docs/adr/`, raíz | — |

Pendiente de A para fases posteriores (no bloquea a B):

- Fase 3: capturas de Grafana del antes y después, cuando B y C emitan sus métricas.
- Fase 4: armar el informe y la sección de arquitectura.
- Opcional: que el BFF use el circuit breaker de C hacia Pagos (hoy espera su timeout de 2 s
  cuando Pagos está caído; ver ADR-006, "Costo aceptado").

## Fase B (Persona B) — qué hacer

Objetivo: la Saga real (escenarios 1 y 2). Contratos: [orquestador](contratos/orquestador.md),
[inscripción](contratos/inscripcion.md) y [cupos](contratos/cupos.md).

### 0. Partir

```bash
git switch main && git pull          # cuando feat/gateway-bff esté integrada en main
git switch -c feat/saga
pip install -r requirements-dev.txt
python -m unittest discover -s tests # 48 pruebas en verde al partir
docker compose up --build -d         # el sistema completo funciona con stubs
```

### 1. Cupos (`cupos/`)

- Agregar `ReservarCupo` / `LiberarReserva` y la tabla `reservas` según
  [contratos/cupos.md](contratos/cupos.md). Mantener el lock y el `CHECK` contra sobrecupo.
- **No cambiar** `ConsultarCurso` ni `ListarCursos`: el BFF ya los usa (`bff-web/app/main.py`).
- Tras cambiar el proto, `python scripts/generar_stubs.py` regenera los stubs de todos los
  servicios listados en `DESTINOS`. Agregar ahí `orquestador/app` y quitar `matriculas/app`
  cuando deje de existir.
- `tests/test_cupos.py` ya prueba el servicio: extenderlo con la idempotencia.

### 2. Inscripción (`inscripcion/`, desde `matriculas/`)

- `git mv matriculas inscripcion` para conservar el historial.
- Quitar gRPC, la caché de cursos y el middleware `bearer_auth` (la auth ahora es del gateway).
- Las rutas internas van **sin** `/v1`: el gateway traduce `/api/v1/estudiantes` → `/estudiantes`.
- Implementar `POST /inscripciones`, `POST /inscripciones/{id}/baja`,
  `GET /inscripciones?estudiante_id=` y `/admin/falla` (se puede copiar `stubs/app/fallas.py`).
- `GET /inscripciones?estudiante_id=` es lo que lee el BFF: respetar los campos del contrato.
- Agregar `instrumentar(app, "inscripcion")` y, en el Dockerfile, `COPY comun ./comun` y
  `prometheus-client==0.26.0` en `requirements.txt` (ver `gateway/Dockerfile` como ejemplo).
- Adaptar `tests/test_api.py` y `tests/test_contracts.py`, que hoy prueban `matriculas/`.

### 3. Orquestador (`orquestador/`)

- `POST /matriculas` y `GET /sagas/{id}` según [contratos/orquestador.md](contratos/orquestador.md).
- Usar `comun.observabilidad`: `instrumentar(app, "orquestador")`, `log.info(..., extra={"saga_id": ...})`,
  `cabeceras_salientes()` en cada llamada HTTP y `metadata_grpc()` en cada llamada a Cupos.
  Así el mismo `correlation_id` aparece en los logs de los 4 servicios.
- Emitir `saga_total{resultado}` (prometheus_client `Counter`).
- `_links` con `X-Forwarded-Prefix` delante; `Location` con la ruta interna (el gateway la traduce).
- **Pagos:** `orquestador/app/pagos_client.py` y `resiliencia.py` son de C. Para no bloquearse,
  B escribe un `pagos_client.py` mínimo (httpx con timeout de 2 s) con esta interfaz, y C
  después le agrega retry, circuit breaker, bulkhead y fallback sin que cambie la firma:

  ```python
  class PagosNoDisponible(Exception): ...         # timeout, caída o circuito abierto -> 503 + Retry-After
  class PagosClient:
      def cobrar(self, cobro_id, estudiante_id, curso_id, monto) -> dict: ...
      def reembolsar(self, cobro_id) -> dict: ...
  ```

  Acordar esta interfaz con C antes de empezar.

  **Hecho (B):** `orquestador/app/pagos_client.py` mínimo con esa interfaz y dos agregados que la
  Saga necesita:
  - `PagosNoDisponible(motivo, puede_haberse_aplicado=True, retry_after=5)`. C debe poner
    `puede_haberse_aplicado=False` cuando la solicitud nunca salió (circuito abierto, bulkhead
    lleno, no conectó): así la Saga solo libera el cupo y no intenta un reembolso innecesario.
    `retry_after` es lo que el orquestador devuelve en la cabecera `Retry-After`.
  - `PagosRechazado(motivo, status)` para los 4xx (el cobro seguro no se aplicó).
- Mientras C no entregue `pagos/`, el orquestador habla con el **stub de Pagos**, que ya es
  idempotente por `cobro_id` y guarda lápida al reembolsar algo inexistente.

### 4. Compose

En `docker-compose.yml`, reemplazar los bloques marcados `TODO(B)` (orquestador e inscripción)
por las imágenes reales, con su volumen para la BD. El resto del archivo no cambia. El
orquestador puede usar Redis (`redis://redis:6379/1`, base distinta a la del rate limit) para
`Idempotency-Key`. Si el peor caso de la Saga supera 15 s, subir `GATEWAY_TIMEOUT_SAGA_SECONDS`.

### 5. Listo cuando

- `POST /api/v1/matriculas` a través del gateway completa la Saga y `GET /api/v1/sagas/{id}`
  muestra los 3 pasos.
- El panel del BFF (`/api/v1/web/alumnos/{id}/panel`) muestra la inscripción y el cobro nuevos.
- Con `/admin/falla` en Inscripción, la saga queda `COMPENSADA` (reembolso y luego liberar cupo)
  y el panel no muestra ni inscripción ni cobro activos.
- Tests del PLAN §4 (B) en verde, ADR-007, diagramas Mermaid y secciones del README
  "Escenario 1" y "Escenario 2" completas.

## Fase C (Persona C) — adelanto

- Reemplazar el bloque `TODO(C)` de `pagos` en el compose. El contrato está en
  [contratos/pagos.md](contratos/pagos.md); el stub (`stubs/app/pagos.py` + `fallas.py`) sirve de
  referencia del comportamiento esperado.
- Métricas `circuit_state`, `bulkhead_in_flight` y `bulkhead_rejected_total` con las etiquetas
  de [contratos/README.md](contratos/README.md#métricas): el dashboard ya tiene sus paneles.
- Para las pruebas de carga usar el token `carga-experimentos` (límite alto), así el rate limit
  del gateway no contamina las mediciones.
