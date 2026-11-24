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

## Fase B (Persona B) — terminada

Todo lo de la Persona B en [PLAN.md](PLAN.md) §4 está hecho, en `main`.

| Qué | Dónde | Cómo se verificó |
|---|---|---|
| Reservas idempotentes con lápida (`ReservarCupo` / `LiberarReserva`) y logs JSON con `saga_id` | `cupos/` | `tests/test_cupos.py` + en vivo |
| Inscripción: refactor de `matriculas/` sin gRPC ni auth, inscripciones idempotentes, `/admin/falla`, `est-001` precargado | `inscripcion/` | `tests/test_api.py` (contra su OpenAPI) + en vivo |
| Orquestador: Saga con compensación en orden inverso, `GET /sagas/{id}`, `saga_total`, `Idempotency-Key` en Redis, worker con backoff | `orquestador/` | `tests/test_orquestador.py`, `tests/test_clientes_orquestador.py` |
| `pagos_client.py` mínimo (solo timeout) con la interfaz para C | `orquestador/app/pagos_client.py` | `tests/test_clientes_orquestador.py` |
| Compose con orquestador e inscripción reales | `docker-compose.yml` | `docker compose up`: todo healthy |
| Escenarios reproducibles | `scripts/escenarios.py` | 1, 2, idempotencia, sin-cupo y pendiente con `Resultado: OK` |
| ADR-007, diagramas Mermaid, README escenarios 1 y 2 | `docs/adr/`, `docs/diagramas/`, `README.md` | — |

Pendiente que toca a otros:

- **C:** reemplazar `pagos_client.py` respetando la interfaz (ver abajo, "Hecho (B)"). Importante
  para el escenario 3: con el circuito abierto o el bulkhead lleno, lanzar
  `PagosNoDisponible(..., puede_haberse_aplicado=False)` para que la Saga solo libere el cupo.
  Si se agregan reintentos, revisar que el peor caso de la saga siga bajo
  `GATEWAY_TIMEOUT_SAGA_SECONDS` (hoy 15 s; sin reintentos el peor caso es ~14 s).
- **A:** el panel de Grafana de sagas ya recibe `saga_total`; capturas para el informe.
- **Informe (B):** la sección de la Saga puede salir de ADR-007 (incluye la tabla de evidencia
  medida) y de `docs/diagramas/saga.md`.

La guía original de la fase B queda abajo como referencia.

## Fase B (Persona B) — guía original

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

## Fase C (Persona C) — terminada (falta tu corrida con Docker y las capturas de Grafana)

| Qué | Dónde | Cómo se verificó |
|---|---|---|
| Pagos real: BD propia, cobro y reembolso idempotentes por `cobro_id`, lápida, `/admin/falla`, métricas | `pagos/` | `tests/test_pagos.py` + en vivo |
| Circuit Breaker (ventana de fallos, half-open) y Bulkhead (semáforo no bloqueante) con métricas | `orquestador/app/resiliencia.py` | `tests/test_resiliencia.py` |
| `PagosClient`: timeout, reintento con backoff (solo `cobrar`), CB, bulkhead, fallback; `RESILIENCIA=off` | `orquestador/app/pagos_client.py` | `tests/test_resiliencia.py`, `tests/test_clientes_orquestador.py` |
| Compose con Pagos real, volumen `pagos_data` y variables de resiliencia | `docker-compose.yml` | ver abajo |
| Experimento del escenario 3 (con y sin patrón) + consistencia final | `experimentos/` | corrida completa de ambos modos |
| ADR-008 | `docs/adr/ADR-008-resiliencia-pagos.md` | — |

La interfaz que dejó B (`PagosNoDisponible(motivo, puede_haberse_aplicado, retry_after)`, `PagosRechazado`) **no cambió**;
solo se agregó el atributo interno `reintentable`. La Saga (`saga.py`) y `main.py` del orquestador no se tocaron.

Pendiente (no se puede hacer sin tu entorno):

- Correr `python experimentos/experimento.py --modo ambos` con `docker compose` y guardar en `docs/resultados/`
  los `escenario3-*.json/png/md` **de tu corrida**: los números del ADR-008 son de una corrida de verificación
  local (sin Docker) y hay que reemplazarlos por los tuyos.
- Capturas de Grafana del antes y después (paneles de circuito, bulkhead y latencia) para el informe.
- Informe: sección de resiliencia a partir del ADR-008; el análisis "sin patrón" lo escriben los tres.

### Decisiones y límites que conviene conocer para la defensa

- `RESILIENCIA=off` deja **solo el timeout**: así la comparación aísla el efecto de CB + bulkhead + reintento.
- Con el circuito abierto el reembolso también se corta: la saga queda `COMPENSACION_PENDIENTE` y el worker la termina
  cuando Pagos vuelve (consistencia eventual, medida en el experimento).
- El **panel del BFF** sigue esperando 2 s a Pagos cuando está lento (el BFF no usa el circuit breaker). Es el
  "costo aceptado" de ADR-006; no es contagio hacia las otras secciones, que responden igual.
- Estado del circuito en memoria del proceso: vale para una sola instancia del orquestador (igual que el worker).
