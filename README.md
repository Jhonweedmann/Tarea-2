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

> **Estado:** gateway, BFF y observabilidad están implementados. `orquestador`, `inscripcion` y
> `pagos` corren todavía como **stubs** en memoria (`stubs/`) que cumplen los contratos; B y C
> los reemplazan. Ver [docs/TRASPASO.md](docs/TRASPASO.md).

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

El estudiante `est-001` (Ana Pérez) viene precargado en el stub de Inscripción, con una
inscripción y un cobro, para que el panel tenga datos desde el inicio.

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

### Escenario 1 — matrícula exitosa (B)

_Pendiente: lo completa B con los comandos para la Saga real (`POST /api/v1/matriculas` →
`GET /api/v1/sagas/{id}` y estado de las 3 BD)._

### Escenario 2 — falla en el paso 3 con compensación (B)

_Pendiente: lo completa B (`/admin/falla` en Inscripción → reembolso y liberación del cupo)._

### Escenario 3 — falla en cascada contenida (C)

_Pendiente: lo completa C (línea base, Pagos lento, Pagos caído, recuperación y corrida sin patrón)._

## Observabilidad

- Cada servicio usa [`comun/observabilidad.py`](comun/observabilidad.py): logs JSON con
  `correlation_id` (y `saga_id` cuando aplica), propagación de `X-Correlation-Id` / `X-Client-Id`
  a HTTP y gRPC, y las métricas `http_request_duration_seconds` y `http_requests_total`.
- Prometheus ([observabilidad/prometheus.yml](observabilidad/prometheus.yml)) recolecta cada 5 s.
  No publica puerto; Grafana lo consulta por la red interna.
- Dashboard provisionado: [observabilidad/grafana/dashboards/forma-k.json](observabilidad/grafana/dashboards/forma-k.json).
  Los paneles de circuito, bulkhead y sagas muestran "No data" hasta que B y C emitan esas
  métricas (nombres en [docs/contratos/README.md](docs/contratos/README.md#métricas)).

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
| `tests/test_api.py`, `test_contracts.py`, `test_cupos.py` | servicios de la Tarea 1 (B los adapta al refactor de Matrículas → Inscripción) |

Los stubs Python de gRPC (`cupos_pb2*.py`) no se versionan: los Dockerfiles los generan desde
`cupos/cupos.proto` y las pruebas los regeneran con `scripts/generar_stubs.py`.

## Contratos y decisiones

- Contratos entre servicios: [docs/contratos](docs/contratos)
- gRPC: [cupos/cupos.proto](cupos/cupos.proto)
- Decisiones: [docs/adr](docs/adr) — ADR-001 a ADR-005 vienen de la Tarea 1;
  [ADR-006](docs/adr/ADR-006-gateway-bff.md) Gateway y BFF.

## Antecedentes (Tarea 1)

`matriculas/` (REST) y `cupos/` (gRPC) son el punto de partida; su contrato REST sigue en
[matriculas/openapi.yaml](matriculas/openapi.yaml) hasta que B lo convierta en `inscripcion/`.
Datos crudos de sus experimentos en [docs/resultados](docs/resultados). El cliente Node de gRPC
sigue disponible con `docker compose --profile demo run --rm cupos-node-client`.

## Uso de IA

Se usó asistencia de IA para estructurar código, contratos y documentación. El equipo debe revisar
el código generado, ejecutar pruebas y explicar las decisiones y resultados de los experimentos.
