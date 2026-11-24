# Forma K — Matrícula en un curso (Unidad 3)

Sistema distribuido para matricular estudiantes en cursos. Parte de la Tarea 1 (Matrículas REST +
Cupos gRPC) y agrega un **API Gateway**, un **BFF web**, una **Saga orquestada** (cupo → cobro →
inscripción) y **resiliencia** (timeout, circuit breaker y bulkhead) frente a Pagos, que simula
una pasarela externa frágil. Plan y reparto: [docs/PLAN.md](docs/PLAN.md). Estado y traspaso
entre integrantes: [docs/TRASPASO.md](docs/TRASPASO.md).

```
  cliente ──8080──▶ gateway (auth · rate limit por cliente · X-Correlation-Id)
                       ├──▶ bff-web ──────▶ inscripcion · cupos (gRPC) · pagos   (panel con degradación parcial)
                       ├──▶ orquestador ──▶ cupos (gRPC) · pagos · inscripcion   (Saga + CB + bulkhead)
                       └──▶ inscripcion                                          (alta de estudiantes)
  redis (rate limit + idempotencia) · prometheus · grafana (:3000)
```

> **Estado:** gateway, BFF, observabilidad (A), la Saga con `orquestador`, `inscripcion` y `cupos` (B),
> y `pagos` con la resiliencia del orquestador hacia él (C) están implementados. Ver
> [docs/TRASPASO.md](docs/TRASPASO.md).

## Cómo levantarlo

**Requisito:** Docker con Docker Compose. Desde la raíz del repositorio:

```bash
docker compose up --build -d
docker compose ps            # todos "healthy" salvo prometheus y grafana, que no tienen healthcheck
```

- API pública: `http://localhost:8080` (único puerto publicado por el sistema).
- Grafana: `http://localhost:3000` → dashboard **Forma K — Resiliencia y plataforma** (acceso
  anónimo de lectura; usuario `admin`, contraseña `forma-k` para editar).
- Para partir de cero: `docker compose down -v`.

## API pública (gateway)

Contrato completo: [docs/contratos/gateway.md](docs/contratos/gateway.md).

| Ruta | Va a | Para qué |
|---|---|---|
| `POST /api/v1/matriculas` (+ `Idempotency-Key`) | orquestador | Ejecuta la Saga de matrícula |
| `GET /api/v1/sagas/{id}` | orquestador | Estado y pasos de una saga |
| `POST/GET /api/v1/estudiantes[/{id}]` | inscripcion | Alta y consulta de estudiantes |
| `GET /api/v1/cursos[/{id}]` | bff-web → cupos | Catálogo con cupos |
| `GET /api/v1/web/alumnos/{id}/panel` | bff-web | Panel del alumno (inscripción + cupos + pagos) |

**Tokens de desarrollo** (`Authorization: Bearer <token>`). El límite es **por cliente**:

| Token | Cliente | Rol | Límite |
|---|---|---|---|
| `desarrollo-seguro` | `secretaria` | escritura | 20 / 10 s |
| `solo-lectura` | `consulta` | solo `GET` | 20 / 10 s |
| `portal-web` | `portal-web` | escritura | 20 / 10 s |
| `carga-experimentos` | `experimentos` | escritura | 100 000 / 10 s (pruebas de carga) |

El estudiante `est-001` (Ana Pérez) viene precargado en Inscripción, y el stub de Pagos trae un
cobro de ejemplo para él, así el panel tiene datos desde el inicio. Cursos: `ARQ-101` (2 cupos),
`API-201` (1), `DAT-110` (3) e `INT-301` (30, para repetir los escenarios en la demo).

Todas las solicitudes del repo también están en [request.http](request.http) (extensión REST
Client de VS Code).

## Escenarios y evidencia

### Gateway y BFF (A)

**401 sin token, 403 con token de solo lectura:**

```bash
curl -i http://localhost:8080/api/v1/cursos
curl -i -X POST http://localhost:8080/api/v1/matriculas -H "Authorization: Bearer solo-lectura" -H "Content-Type: application/json" -d '{}'
```

**429 al superar el límite, sin afectar a otro cliente** (25 solicitudes de `portal-web` → 20
responden 200 y 5 responden 429 con `Retry-After`; `secretaria` sigue respondiendo 200):

```bash
for i in $(seq 1 25); do curl -s -o /dev/null -w "%{http_code} " -H "Authorization: Bearer portal-web" http://localhost:8080/api/v1/cursos; done; echo
curl -s -o /dev/null -w "otro cliente: %{http_code}\n" -H "Authorization: Bearer desarrollo-seguro" http://localhost:8080/api/v1/cursos
```

```powershell
1..25 | ForEach-Object { try { (Invoke-WebRequest http://localhost:8080/api/v1/cursos -Headers @{Authorization='Bearer portal-web'} -UseBasicParsing).StatusCode } catch { [int]$_.Exception.Response.StatusCode } }
```

**Panel del BFF con Pagos caído** (responde `200` con `"pagos": {"disponible": false, ...}` y
`"degradado": true`; el resto del panel llega completo):

```bash
docker compose stop pagos
curl -s -H "Authorization: Bearer desarrollo-seguro" http://localhost:8080/api/v1/web/alumnos/est-001/panel
docker compose start pagos
```

Pagos lento en vez de caído (el panel igual responde en ~2 s, el tiempo máximo del BFF):

```bash
docker compose exec pagos python -c "import httpx; print(httpx.post('http://localhost:8000/admin/falla', json={'latencia_ms': 5000}).json())"
docker compose exec pagos python -c "import httpx; print(httpx.post('http://localhost:8000/admin/falla', json={'latencia_ms': 0, 'caido': False}).json())"
```

**Trazar una solicitud por todos los servicios** con su `X-Correlation-Id` (logs JSON):

```bash
curl -s -H "Authorization: Bearer desarrollo-seguro" -H "X-Correlation-Id: demo-1" http://localhost:8080/api/v1/web/alumnos/est-001/panel > /dev/null
docker compose logs | grep demo-1
```

### Saga de matrícula (B)

La matrícula es una **Saga orquestada**: reservar cupo → cobrar → inscribir, con el `saga_id`
como clave idempotente en cada servicio y compensación en orden inverso si algo falla. Decisión
en [ADR-007](docs/adr/ADR-007-saga-orquestada.md), diagramas en
[docs/diagramas/saga.md](docs/diagramas/saga.md) y contrato en
[docs/contratos/orquestador.md](docs/contratos/orquestador.md).

Cada escenario se reproduce con un comando (con el sistema levantado y las dependencias de
`requirements-dev.txt`; en Windows, `.\.venv\Scripts\python` en vez de `python`). El script
crea un estudiante nuevo en cada corrida, muestra la respuesta, el historial de la saga y el
estado final en Inscripción, Pagos y Cupos, y termina con `Resultado: OK` si todo cuadra:

```bash
python scripts/escenarios.py 1             # escenario 1: éxito
python scripts/escenarios.py 2             # escenario 2: falla el paso 3 y se compensa
python scripts/escenarios.py idempotencia  # misma Idempotency-Key dos veces: un solo cobro
python scripts/escenarios.py sin-cupo      # sin cupo: falla el paso 1 y no se cobra
python scripts/escenarios.py pendiente     # una compensación falla y el worker la termina
```

### Escenario 1 — matrícula exitosa (B)

```bash
curl -s -i -X POST http://localhost:8080/api/v1/matriculas \
  -H "Authorization: Bearer desarrollo-seguro" -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-1" -H "X-Correlation-Id: esc-1" \
  -d '{"estudiante_id": "est-001", "curso_id": "INT-301"}'
```

Responde `201` con `Location: /api/v1/sagas/{id}` y la saga en estado `COMPLETADA`, con los pasos
`validar_estudiante`, `reservar_cupo`, `cobrar` e `inscribir` en `ok`. Repetir el mismo comando
(misma `Idempotency-Key`) responde `200` con `Idempotent-Replay: true` y **no vuelve a cobrar**.

Evidencia:

```bash
curl -s -H "Authorization: Bearer desarrollo-seguro" http://localhost:8080/api/v1/sagas/<saga_id>
curl -s -H "Authorization: Bearer desarrollo-seguro" http://localhost:8080/api/v1/web/alumnos/est-001/panel
docker compose logs orquestador cupos pagos inscripcion | grep esc-1
```

El panel muestra la inscripción `activa`, el cobro `cobrado` y un cupo menos en `INT-301`. Los
logs muestran la misma saga (`correlation_id` y `saga_id`) en los cuatro servicios.

### Escenario 2 — falla en el paso 3 con compensación (B)

Se hace fallar el **último** paso poniendo Inscripción en modo caído (su `/admin/falla` no es
pública, se llama dentro del contenedor):

```bash
FALLA='import sys,urllib.request as u; print(u.urlopen(u.Request("http://localhost:8000/admin/falla", data=sys.argv[1].encode(), headers={"Content-Type": "application/json"})).read().decode())'
docker compose exec -T inscripcion python -c "$FALLA" '{"caido": true}'

curl -s -i -X POST http://localhost:8080/api/v1/matriculas \
  -H "Authorization: Bearer desarrollo-seguro" -H "Content-Type: application/json" \
  -H "X-Correlation-Id: esc-2" -d '{"estudiante_id": "est-001", "curso_id": "DAT-110"}'

docker compose exec -T inscripcion python -c "$FALLA" '{"caido": false}'
```

Responde `502` (problem+json) con el `saga_id` y estado `COMPENSADA`. En `GET /api/v1/sagas/{id}`
se ve el orden inverso: `inscribir error` → `reembolsar ok` → `liberar_cupo ok`. En el panel el
cobro de esa saga aparece `reembolsado`, no hay inscripción activa y `DAT-110` conserva sus cupos:
**ni cobro sin inscripción, ni reserva colgada**. Con Inscripción **lenta** en vez de caída
(`{"latencia_ms": 3000}`) el paso 3 queda incierto, también se da de baja, y si la compensación
falla la saga queda en `COMPENSACION_PENDIENTE` hasta que el worker la termina
(`python scripts/escenarios.py pendiente`).

### Escenario 3 — falla en cascada contenida (C)

Pagos es la dependencia frágil (pasarela externa). El orquestador la llama con **timeout de 2 s,
reintento con backoff (solo `cobrar`), circuit breaker y bulkhead de 5 llamadas**, y con el circuito
abierto responde un *fallback* de milisegundos: `503` + `Retry-After`, liberando el cupo
([ADR-008](docs/adr/ADR-008-resiliencia-pagos.md)).

```bash
docker compose up --build -d
python experimentos/experimento.py --modo ambos     # ~6 min: con patrones y "sin patrón" (RESILIENCIA=off)
```

El script recorre línea base → Pagos lento (con un pico de 30 matrículas/s) → Pagos caído → recuperación,
mide a la vez la salud del resto del sistema (`GET /sagas/{id}`, `GET /cursos` y el panel del BFF) y
guarda en `docs/resultados/` los datos crudos (`escenario3-*.json`), un gráfico por corrida y la
comparación (`escenario3-comparacion.md`). Detalle y cómo leer la evidencia:
[experimentos/README.md](experimentos/README.md).

**Para hacerlo a mano** (por ejemplo durante el video), con Grafana abierto en http://localhost:3000:

```bash
# 1. Pagos lento: 5 s por llamada
docker compose exec pagos python -c "import httpx; print(httpx.post('http://localhost:8000/admin/falla', json={'latencia_ms': 5000}).json())"
# 2. Pagos caído (503)
docker compose exec pagos python -c "import httpx; print(httpx.post('http://localhost:8000/admin/falla', json={'latencia_ms': 0, 'caido': True}).json())"
# 3. Recuperación
docker compose exec pagos python -c "import httpx; print(httpx.post('http://localhost:8000/admin/falla', json={'caido': False}).json())"
# Matrícula (observar el tiempo de respuesta y el Retry-After)
curl -s -o /dev/null -w "%{http_code} en %{time_total}s\n" -X POST localhost:8080/api/v1/matriculas \
  -H "Authorization: Bearer desarrollo-seguro" -H "Content-Type: application/json" \
  -d '{"estudiante_id":"est-001","curso_id":"INT-301"}'
# Estado del circuito y del bulkhead (métricas internas del orquestador)
docker compose exec orquestador python -c "import urllib.request as u; print(''.join(l for l in u.urlopen('http://localhost:8000/metrics').read().decode().splitlines(True) if l.startswith(('circuit_','bulkhead_','pagos_llamadas'))))"
```

Qué esperar con los patrones puestos: la primera ráfaga contra el Pagos lento agota 2 s de timeout y
el circuito abre; desde entonces cada matrícula responde en decenas de milisegundos con `503` +
`Retry-After`, el cupo queda liberado, y `GET /cursos`, `GET /sagas/{id}` y el panel siguen rápidos.
Al volver Pagos, el circuito pasa por half-open y cierra, y el worker termina las compensaciones pendientes.

## Observabilidad

- Cada servicio usa [`comun/observabilidad.py`](comun/observabilidad.py): logs JSON con
  `correlation_id` (y `saga_id` cuando aplica), propagación de `X-Correlation-Id` / `X-Client-Id`
  a HTTP y gRPC, y las métricas `http_request_duration_seconds` y `http_requests_total`.
- Prometheus ([observabilidad/prometheus.yml](observabilidad/prometheus.yml)) recolecta cada 5 s.
  No publica puerto; Grafana lo consulta por la red interna.
- Dashboard provisionado: [observabilidad/grafana/dashboards/forma-k.json](observabilidad/grafana/dashboards/forma-k.json).
  El orquestador emite `saga_total{resultado}` (panel de sagas por resultado) y, para la
  resiliencia hacia Pagos, `circuit_state`, `bulkhead_in_flight`, `bulkhead_rejected_total`,
  `circuit_rejected_total`, `pagos_llamadas_total` y `pagos_reintentos_total` (nombres en
  [docs/contratos/README.md](docs/contratos/README.md#métricas)).

## Pruebas

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

En Windows con el entorno virtual del proyecto: `.\.venv\Scripts\python` en vez de `python`.

| Archivo | Qué prueba |
|---|---|
| `tests/test_gateway.py` | auth 401/403, rate limit por cliente y fail-open sin Redis, reenvío y cabeceras, `Location` público, 502/504, rechazo de `..` |
| `tests/test_bff.py` | panel completo, degradación con Pagos caído/lento/503 y con Cupos caído, 404/503/504 del estudiante |
| `tests/test_orquestador.py` | Saga: camino feliz, falla del paso 3 con compensación en orden inverso, sin cupo sin cobro, Pagos no disponible, compensación pendiente, `Idempotency-Key` (un solo cobro) y worker con backoff |
| `tests/test_clientes_orquestador.py` | cómo los clientes de Pagos e Inscripción clasifican cada falla (¿pudo aplicarse o no?) |
| `tests/test_resiliencia.py` | Circuit Breaker (abre, half-open, cierra), Bulkhead, y el cliente de Pagos: reintento con backoff, fallback con el circuito abierto, incertidumbre arrastrada entre intentos, `RESILIENCIA=off` |
| `tests/test_pagos.py` | Pagos: cobro idempotente por `cobro_id` (incluso con reintentos simultáneos), reembolso con lápida, `/admin/falla` |
| `tests/test_api.py` | Inscripción contra su `openapi.yaml`: estudiantes, inscripciones idempotentes, lápidas, `/admin/falla` |
| `tests/test_cupos.py`, `test_contracts.py` | reservas idempotentes de Cupos (con lápida) y contratos `.proto` / OpenAPI |

Los stubs Python de gRPC (`cupos_pb2*.py`) no se versionan: los Dockerfiles los generan desde
`cupos/cupos.proto` y las pruebas los regeneran con `scripts/generar_stubs.py`.

## Contratos y decisiones

- Contratos entre servicios: [docs/contratos](docs/contratos)
- gRPC: [cupos/cupos.proto](cupos/cupos.proto)
- Decisiones: [docs/adr](docs/adr) — ADR-001 a ADR-005 vienen de la Tarea 1;
  [ADR-006](docs/adr/ADR-006-gateway-bff.md) Gateway y BFF;
  [ADR-007](docs/adr/ADR-007-saga-orquestada.md) Saga orquestada (y no 2PC ni coreografía);
  [ADR-008](docs/adr/ADR-008-resiliencia-pagos.md) Resiliencia hacia Pagos (timeout, reintento, circuit breaker, bulkhead).
- Diagramas de la Saga (Mermaid): [docs/diagramas/saga.md](docs/diagramas/saga.md)

## Antecedentes (Tarea 1)

`matriculas/` (REST, renombrado a `inscripcion/`) y `cupos/` (gRPC) son el punto de partida; su
contrato REST está en [inscripcion/openapi.yaml](inscripcion/openapi.yaml).
Datos crudos de sus experimentos en [docs/resultados](docs/resultados). El cliente Node de gRPC
sigue disponible con `docker compose --profile demo run --rm cupos-node-client`.

## Uso de IA

Se usó asistencia de IA para estructurar código, contratos y documentación. El equipo debe revisar
el código generado, ejecutar pruebas y explicar las decisiones y resultados de los experimentos.

- **Parte de B (Saga):** se usó Claude Code (Anthropic) para implementar las reservas idempotentes
  de Cupos, el refactor de Matrículas a Inscripción, el orquestador (Saga, Idempotency-Key y
  worker), el script de escenarios, ADR-007 y los diagramas. Se verificó con las pruebas
  automáticas (`tests/`) y ejecutando los escenarios contra el sistema completo en Docker.
