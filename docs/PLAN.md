# Plan — Encargo Unidad 3, Forma K (matrícula en un curso)

Este repo parte de la Tarea 1: se conserva su historial y el código se reutiliza.
El objetivo es cubrir el encargo de la Unidad 3: Gateway + BFF, Saga, Circuit Breaker +
Bulkhead, y evidencia de cómo responde el sistema ante fallas.

---

## 1. Arquitectura objetivo

```
                    ┌──────────────┐
  cliente ──8080──▶ │   gateway    │  auth (Bearer) · rate limit por cliente · X-Correlation-Id
                    └──────┬───────┘
              ┌────────────┴─────────────┐
              ▼                          ▼
       ┌─────────────┐            ┌─────────────┐
       │   bff-web   │            │ orquestador │  Saga (log en su BD)
       │ (agregación)│            │             │  timeout · CB · bulkhead → Pagos
       └──┬───┬───┬──┘            └──┬────┬───┬─┘
          │   │   │        gRPC      │    │   │
          │   │   └─────────────▶ cupos ◀─┘   │        (cupos.db)
          │   └────────────────▶ inscripcion ◀┘        (inscripcion.db)
          └────────────────────▶ pagos ◀──────── (CB + bulkhead)   (pagos.db)
                                  ▲
                         dependencia frágil (latencia / caída inyectables)

  redis (rate limit + idempotencia) · prometheus · grafana
```

Solo el gateway publica un puerto (8080). Grafana puede publicar el 3000 solo para la demo.

### Qué se reutiliza de la Tarea 1

| De la Tarea 1 | Pasa a ser |
|---|---|
| `cupos/` (gRPC, SQLite, lock contra sobrecupo) | Servicio **Cupos**. Se le agrega idempotencia por `reserva_id`. |
| `matriculas/` (estudiantes + matrículas) | Servicio **Inscripción**. Se quitan las llamadas a Cupos: inscribir y dar de baja quedan como operaciones locales. |
| `matriculas/app/grpc_client.py` (deadline de 2 s) | Cliente de Cupos del **orquestador**. |
| Middleware `bearer_auth` y respuestas `problem+json` de `main.py` | Se trasladan al **gateway**. Los servicios dejan de autenticar. |
| Idempotencia con Redis (`SET NX`) | Se reutiliza en el endpoint de la Saga (`Idempotency-Key`). |
| ADR-001 a ADR-005 | Se mantienen como antecedentes. ADR-004 ya anticipa el circuit breaker. |

Nota: el README de la Tarea 1 menciona `docs/informe.tex`, `docs/experimento_resiliencia.py` y
`docs/guion-video.md`, pero esos archivos no están en el repo. Si alguien los tiene en local,
conviene recuperarlos, porque el experimento de resiliencia sirve de base para el escenario 3.

---

## 2. Decisiones de diseño (propuestas, a confirmar en grupo)

| Decisión | Propuesta | Por qué (base para el informe y la defensa) |
|---|---|---|
| Saga: coreografía u orquestación | **Orquestación** | Son 3 pasos lineales con compensación en orden inverso. Un orquestador deja el orden explícito en un solo lugar, guarda el estado de cada saga en un log consultable (eso mismo sirve de evidencia) y no necesita un broker de mensajes. La coreografía repartiría la lógica de compensación entre los 3 servicios y exigiría RabbitMQ o Kafka. |
| Saga y no 2PC | Saga | Pagos es una pasarela externa: no participa en un coordinador XA ni puede quedar "preparada" con bloqueos. Con 2PC, si Pagos queda lento, el cupo se mantiene bloqueado todo ese tiempo. La Saga acepta consistencia eventual y deshace con compensaciones (reembolso). |
| Gateway | **FastAPI propio** (proxy con `httpx`) | Se reutiliza el middleware de auth que ya existe y el equipo puede explicar cada línea en la defensa. Alternativas a mencionar en el ADR: Kong DB-less o Nginx (`limit_req`). |
| Rate limiting | Ventana fija por token en Redis (`INCR` + `EXPIRE`) → `429` con `Retry-After` | Simple, distribuido y fácil de mostrar en vivo. |
| Fallback con el circuito abierto | Fail-fast (`503` + `Retry-After`), **liberar el cupo** y dejar la saga en `ABORTADA_PAGOS_NO_DISPONIBLE` | La regla de negocio prohíbe inscribir sin cobro, así que no se puede "aceptar igual". En el BFF la degradación es parcial: el panel responde con la sección de pagos marcada como no disponible. |
| Bulkhead | Semáforo con adquisición no bloqueante y un pool de hilos dedicado a Pagos (p. ej. 5 en vuelo) → rechazo inmediato del exceso | Si Pagos se pone lento, solo se ocupan esos 5 cupos y el resto de los endpoints (consultas, BFF) sigue respondiendo. |
| Circuit Breaker | Librería (p. ej. `pybreaker`, versión no verificada) o implementación propia de ~40 líneas (CLOSED/OPEN/HALF_OPEN) | Una implementación propia es más fácil de defender. Lo importante es exponer el estado como métrica. |
| Observabilidad | `prometheus_client` en cada servicio + Prometheus + Grafana. Logs JSON con `correlation_id` y `saga_id` | La rúbrica pide capturas del antes y después: estado del circuito, rechazos del bulkhead y latencias. |

### Idempotencia por paso (regla: "reintentar no cobra dos veces")

El orquestador genera un `saga_id` y lo envía como clave en cada paso:

| Paso | Llamada | Compensación | Clave idempotente |
|---|---|---|---|
| 1 | `cupos.ReservarCupo(reserva_id=saga_id, curso_id)` | `LiberarReserva(reserva_id)` | tabla `reservas(reserva_id PK, estado)` en Cupos |
| 2 | `POST pagos/cobros {cobro_id=saga_id, ...}` | `POST pagos/cobros/{id}/reembolso` | `cobro_id` PK en Pagos; reembolso no-op si ya está reembolsado |
| 3 | `POST inscripcion/inscripciones {inscripcion_id=saga_id, ...}` | `POST .../{id}/baja` | `inscripcion_id` PK en Inscripción |

Si se repite una operación con la misma clave, se devuelve el resultado ya guardado. Si se pide
compensar algo que nunca se ejecutó, la compensación es un no-op exitoso. Este segundo caso
cubre el timeout ambiguo: la llamada pudo o no haberse aplicado.

### Estados de la saga (tabla `sagas` en la BD del orquestador)

`INICIADA → CUPO_RESERVADO → PAGO_COBRADO → COMPLETADA`
En caso de fallo: `COMPENSANDO → COMPENSADA`. Si falla una compensación, la saga queda en
`COMPENSACION_PENDIENTE` y un worker la reintenta con backoff hasta lograrlo. Hay que tener
preparada esta explicación para la defensa.

---

## 3. Contratos a acordar el día 0 (antes de repartirse)

Esto se hace **los tres juntos**. Una vez fijados los contratos, cada uno puede avanzar sin
depender de los demás.

1. `cupos/cupos.proto`: `ReservarCupo` / `LiberarReserva` con `reserva_id`, y se conservan `ConsultarCurso` y `ListarCursos`.
2. `pagos` (REST): `POST /cobros`, `POST /cobros/{id}/reembolso`, `GET /cobros?alumno_id=`, `POST /admin/falla` (latencia_ms, caido, tasa_error).
3. `inscripcion` (REST): `POST /inscripciones`, `POST /inscripciones/{id}/baja`, `GET /estudiantes/{id}`, `GET /inscripciones?estudiante_id=`, `POST /admin/falla`.
4. `orquestador` (REST): `POST /matriculas` (con `Idempotency-Key`) y `GET /sagas/{id}` (estado y pasos).
5. Cabeceras comunes: `X-Correlation-Id` (lo genera el gateway) y `X-Client-Id` (lo pone el gateway tras autenticar).
6. Nombres de métricas: `circuit_state{dep}`, `bulkhead_rejected_total{dep}`, `bulkhead_in_flight{dep}`, `http_request_duration_seconds{service,route}`, `saga_total{resultado}`.

Cada contrato va en `docs/contratos/` (OpenAPI o markdown corto). **Hecho:** borrador de A, a validar por B y C.

---

## 4. Reparto en 3 personas

Cada persona trabaja en sus propias carpetas para no pisarse, en su propia rama
(`feat/<tema>`), y se integra con PR o merge a `main`. Así los commits quedan identificables por
integrante. Antes de empezar, cada uno debe revisar su `git config user.name / user.email`:
en el historial de la Tarea 1, un mismo integrante aparece con dos correos distintos.

### Persona A — Exposición y plataforma (Gateway, BFF, observabilidad)

**Estado: terminada** (rama `feat/gateway-bff`). Detalle y siguientes pasos en [TRASPASO.md](TRASPASO.md).
Las rutas públicas quedaron bajo `/api/v1/...` (ADR-003).

**Carpetas:** `gateway/`, `bff-web/`, `observabilidad/`, `comun/`, `stubs/`, `docker-compose.yml`, `README.md`

- [x] `gateway/`: proxy FastAPI. Enruta `/api/matriculas/**` al orquestador, `/api/web/**` al BFF y `/api/cursos/**` a Cupos (o vía BFF).
- [x] Auth central: mover `bearer_auth` desde `matriculas/app/main.py`. 401/403 en `problem+json`. Los servicios internos dejan de validar el token.
- [x] Rate limit por cliente con Redis → `429` + `Retry-After`. Usar un token distinto por cliente para mostrar que el límite es por cliente.
- [x] Generar y propagar `X-Correlation-Id`.
- [x] `bff-web/`: `GET /web/alumnos/{id}/panel` que junta estudiante e inscripciones (Inscripción), cursos y cupos (Cupos) y cobros (Pagos) en una sola respuesta. Con timeout por llamada y **degradación parcial** (`"pagos": {"disponible": false}`) si Pagos falla.
- [x] `docker-compose.yml`: red interna, solo el gateway expuesto, healthchecks y servicios nuevos (Prometheus y Grafana).
- [x] `observabilidad/`: `prometheus.yml` y dashboard de Grafana provisionado (paneles: estado del circuito, rechazos del bulkhead, p95 por servicio, sagas por resultado, 429 del gateway).
- [x] ADR-006 Gateway y BFF (por qué uno propio y no Kong/Nginx; el gateway no tiene lógica de negocio).
- [x] README: cómo levantar el sistema y cómo provocar cada escenario (lo completan B y C con sus comandos).

**Evidencia que aporta:** 401 sin token, 429 al superar el límite y panel del BFF con Pagos caído.

### Persona B — Transacción distribuida (Saga)

**Estado: terminada.** Detalle y verificación en [TRASPASO.md](TRASPASO.md).

**Carpetas:** `orquestador/` (excepto `pagos_client.py` y `resiliencia.py`), `cupos/`, `inscripcion/` (renombrado desde `matriculas/`)

- [x] `cupos/`: tabla `reservas`, `ReservarCupo` / `LiberarReserva` idempotentes por `reserva_id`. Conservar el lock y el `CHECK` contra sobrecupo.
- [x] `inscripcion/`: refactor de `matriculas/`. Inscribir y dar de baja idempotentes por `inscripcion_id`, quitar gRPC y auth, y agregar `/admin/falla` para hacer fallar el paso 3.
- [x] `orquestador/`: `POST /matriculas` ejecuta los 3 pasos, persiste cada transición en la tabla `sagas` y compensa en orden inverso. `GET /sagas/{id}` muestra el historial de pasos.
- [x] Worker de reintento para `COMPENSACION_PENDIENTE` (backoff exponencial).
- [x] Reutilizar la idempotencia con Redis de la Tarea 1 en `POST /matriculas`.
- [x] Tests: camino feliz; fallo en el paso 3 → reembolso y luego liberar cupo; el mismo `Idempotency-Key` dos veces → un solo cobro; sin cupo → falla en el paso 1 sin cobrar.
- [x] ADR-007 Saga orquestada vs coreografía y Saga vs 2PC.
- [x] Diagramas de secuencia (Mermaid): flujo feliz y fallo en el paso 3 con compensación.

**Escenarios a su cargo:** **1 (éxito)** y **2 (fallo con compensación)**. Evidencia: `GET /sagas/{id}`, logs con `saga_id` en los 3 servicios y estado final de las 3 BD (sin cobro huérfano ni reserva colgada).

### Persona C — Pagos y resiliencia (lo que más pesa: ABET 6, 40%)

**Carpetas:** `pagos/`, `orquestador/app/pagos_client.py`, `orquestador/app/resiliencia.py`, `experimentos/`

- [ ] `pagos/`: servicio con BD propia, cobro y reembolso idempotentes por `cobro_id`, y `/admin/falla` para inyectar latencia, caída o tasa de error. Simula la pasarela externa.
- [ ] `resiliencia.py`: Circuit Breaker (umbral de fallos, ventana, half-open) y Bulkhead (semáforo + pool dedicado), con métricas.
- [ ] `pagos_client.py`: timeout + retry con backoff (solo para errores reintentables; seguro porque el cobro es idempotente) + CB + bulkhead + fallback. Es lo que llama el orquestador en el paso 2.
- [ ] Timeouts en **todas** las llamadas entre servicios (revisar las de A y B).
- [ ] `experimentos/`: script de carga (k6 en un contenedor con `profiles: ["demo"]`, o Python con `httpx` + hilos) y guion reproducible de cada fase:
  1. línea base (Pagos sano);
  2. Pagos lento (p. ej. 5 s) → el bulkhead rechaza el exceso y `/api/web/...` y las consultas siguen rápidas;
  3. Pagos caído → el circuito abre y la respuesta baja de ~timeout a ms (fail-fast + fallback);
  4. recuperación → half-open → cerrado.
- [ ] **Corrida "sin patrón"** (flag `RESILIENCIA=off`) para comparar. La rúbrica pide explicar qué habría pasado sin el patrón, y una medición lo demuestra mejor que un argumento.
- [ ] ADR-008 Resiliencia (valores de timeout, umbral y tamaño del bulkhead, y su justificación).
- [ ] Datos crudos en `docs/resultados/` + capturas de Grafana del antes y después.

**Escenario a su cargo:** **3 (falla en cascada contenida)**.

### Compartido (los tres)

- **Día 0:** contratos (sección 3) y esqueleto del `docker-compose.yml` con todos los servicios devolviendo `/health`.
- **Informe (≤ 8 págs.):** cada uno escribe la sección de su tema. A arma el documento y la arquitectura, B la Saga y sus diagramas, C la resiliencia y la evidencia, y el análisis "sin patrón" se escribe en conjunto.
- **Video (≤ 8 min, participan todos):** A presenta la arquitectura y el gateway/BFF (~2 min), B los escenarios 1 y 2 (~2,5 min) y C el escenario 3 con Grafana (~3 min).
- **Defensa oral:** cada uno debe poder explicar el código de los otros dos a grandes rasgos, porque se pregunta "por cualquier línea".

---

## 5. Fases

| Fase | Qué | Quién | Listo cuando |
|---|---|---|---|
| 0 | Contratos y esqueleto del compose | Todos | `docker compose up` levanta todos los `/health` |
| 1 | Construcción en paralelo | A / B / C | Cada uno tiene su parte con tests y funcionando sola |
| 2 | Integración | Todos | Escenario 1 de punta a punta a través del gateway |
| 3 | Experimentos y evidencia | C (lidera), A (dashboards), B (escenario 2) | Los 3 escenarios reproducibles con un comando cada uno |
| 4 | Informe, video y ensayo de la defensa | Todos | Entregables listos |

Dependencias críticas: B necesita a C (Pagos) recién en la fase 2; mientras tanto puede usar un
stub. A necesita el endpoint del orquestador para enrutarlo, pero puede probar con un servicio de
eco.

---

## 6. Checklist contra la rúbrica

- [x] Gateway: enruta, autentica y limita · BFF: agrega ≥ 2 servicios (ABET 1)
- [x] Saga: camino feliz y compensación en orden inverso (ABET 1)
- [ ] Timeout + CB con fallback + Bulkhead sobre Pagos (ABET 1)
- [x] Justificación de orquestación vs coreografía y de Saga vs 2PC (ABET 2)
- [x] Diagramas del flujo feliz y de las fallas (ABET 2)
- [x] Idempotencia en pasos y compensaciones · gateway sin lógica de negocio (ABET 2)
- [ ] Escenarios reproducibles: lentitud, caída, pico de carga (ABET 6)
- [ ] Evidencia del antes/después: métricas, logs, estado del circuito, rechazos (ABET 6)
- [ ] Interpretación: qué muestra cada gráfico y qué pasaría sin el patrón (ABET 6)
