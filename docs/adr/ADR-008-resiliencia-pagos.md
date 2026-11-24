# ADR-008 · Resiliencia hacia Pagos: timeout, reintento, circuit breaker y bulkhead

**Estado:** aceptada

## Contexto

Pagos simula una pasarela de pago externa: es la dependencia frágil del dominio y se degrada justo en periodos de
matrícula. El orquestador la llama en el paso 2 de la Saga (`cobrar`) y en su compensación (`reembolsar`). Cada
llamada ocupa un hilo del orquestador (FastAPI atiende los endpoints sincrónicos en un pool de 40 hilos, el mismo que
atiende `GET /sagas/{id}`). Si Pagos tarda 5 s y llegan 30 matrículas por segundo, el pool se llena en un segundo y
el orquestador deja de responder **todo**, no solo las matrículas: una falla local se vuelve cascada (el gateway
también acumula conexiones hasta devolver 504). La regla de negocio impide la salida fácil: no se puede inscribir
sin cobro, así que "aceptar igual" no es opción.

## Alternativas consideradas

- **Solo timeout:** acota cada llamada, pero con 30 matrículas/s y 2 s de timeout siguen ocupándose ~60 hilos.
- **Reintentar sin límite:** multiplica la carga sobre una pasarela que ya está mal.
- **Colas / procesamiento asíncrono del cobro:** cambiaría el contrato de la operación (el cliente no sabría si
  quedó matriculado) y agrega un broker; fuera de alcance.
- **Librería (`pybreaker`, `tenacity`):** válida, pero se prefirió una implementación propia de ~150 líneas, que el
  equipo puede explicar línea por línea en la defensa.

## Decisión

Cada llamada a Pagos atraviesa, de afuera hacia adentro, **reintento → circuit breaker → bulkhead → HTTP con timeout**
(`orquestador/app/pagos_client.py`; piezas en `orquestador/app/resiliencia.py`). Todos los valores son variables de
entorno del compose.

| Mecanismo | Valor | Por qué ese valor |
|---|---|---|
| Timeout | connect 1 s, lectura 2 s (`PAGOS_TIMEOUT_SECONDS`) | Pagos sano responde en ms; 2 s deja holgura sin retener hilos. Coincide con los timeouts de Cupos e Inscripción. |
| Reintento | 1 reintento, solo `cobrar`, backoff 0,2 s × 2ⁿ con jitter, solo ante timeout, error de red, 500/502/504 | Seguro porque el cobro es idempotente por `cobro_id`. No se reintenta el 503 (Pagos dice que está caído) ni `reembolsar` (si falla, el worker de la Saga lo reintenta con backoff sin tener a nadie esperando). |
| Circuit breaker | abre con ≥ 50 % de fallos sobre ≥ 5 llamadas de los últimos 10 s; abierto 5 s; half-open con 1 sonda a la vez, 2 éxitos para cerrar | 5 s > timeout de 2 s: cuando prueba de nuevo ya terminaron las llamadas que estaban en vuelo al abrir. Un 4xx cuenta como éxito (Pagos respondió). |
| Bulkhead | 5 llamadas en vuelo (`PAGOS_BULKHEAD_MAX`), el exceso se rechaza al instante | Pagos sano atiende cobros en ms: 5 simultáneos sobran. Si se pone lento, como máximo 5 de los 40 hilos quedan ocupados. Es un semáforo con adquisición no bloqueante: esperar un lugar dejaría el hilo colgado. |
| Fallback (circuito abierto o bulkhead lleno) | `PagosNoDisponible(puede_haberse_aplicado=False, retry_after)` en ms | La Saga libera el cupo y responde `503` + `Retry-After` (segundos que faltan para la próxima sonda). Es la degradación elegante posible: no se matricula sin cobro, pero se falla rápido y con una indicación clara. |

Dos detalles de corrección que no son opcionales:

1. **La incertidumbre se arrastra entre intentos.** Si el primer intento da timeout (el cobro pudo aplicarse) y el
   reintento lo corta el circuito recién abierto, el cobro sigue siendo incierto: el error final conserva
   `puede_haberse_aplicado=True` y la Saga reembolsa. Sin esto quedaría un cobro huérfano (test
   `test_la_incertidumbre_de_un_intento_se_arrastra...`).
2. **El reembolso también pasa por el circuito.** Con Pagos caído no tiene sentido gastar hilos en reembolsos que van a
   fallar: la saga queda `COMPENSACION_PENDIENTE` y el worker la termina cuando el circuito cierra. Si la llamada
   se aplicó tarde en Pagos, el reembolso posterior la deshace; si el reembolso llegó antes, deja una lápida y el
   cobro atrasado recibe `409`.

`RESILIENCIA=off` desactiva reintento, circuit breaker y bulkhead (queda el timeout). Existe solo para la corrida
"sin patrón" del escenario 3, que aísla el efecto de los tres patrones.

## Timeouts de todas las llamadas entre servicios (revisión)

| Llamada | Límite |
|---|---|
| gateway → servicios | 5 s (15 s la Saga), connect 2 s |
| gateway → Redis | 0,5 s |
| bff → Inscripción, Pagos y Cupos | 2 s cada una, en paralelo |
| orquestador → Cupos (gRPC) | deadline 2 s |
| orquestador → Inscripción | 2 s |
| orquestador → Pagos | connect 1 s, lectura 2 s |
| orquestador → Redis | 0,5 s |

Peor caso de una saga con Pagos roto: `cobrar` (2 s + 0,2 s + 2 s) + `reembolsar` (2 s) + `liberar` (2 s) ≈ 8,2 s; teórico con
todo colgado ≈ 12 s, bajo los 15 s del gateway.

## Evidencia

Experimento reproducible: `python experimentos/experimento.py --modo ambos` (ver `experimentos/README.md`). Carga
abierta de 30 matrículas/s con Pagos lento (5 s) y luego caído, y tres sondas sobre el resto del sistema. La tabla
siguiente es de una **corrida de verificación local, sin Docker, en una sola máquina**: sirve para ver la forma
del resultado. **Reemplazar por la corrida con `docker compose` del equipo** (datos en `docs/resultados/`).

| Fase | Métrica | Con patrones | Sin patrones |
|---|---|---:|---:|
| Pagos lento | `POST /matriculas` p95 | 102 ms | 18 688 ms |
| Pagos lento | respuestas < 250 ms | 98 % | 0 % |
| Pagos lento | sonda `GET /sagas/{id}` p95 | 34 ms | 9 133 ms |
| Pagos lento | sonda `GET /cursos` p95 | 37 ms | 5 067 ms |
| Pagos caído | `POST /matriculas` p95 | 64 ms | 21 374 ms |
| Pagos caído | sonda `GET /sagas/{id}` p95 | 35 ms | 11 853 ms |
| Pagos caído | llamadas que llegaron a Pagos | 1 | 397 |
| Pagos lento/caído | cupos ocupados (máx.; incluye matrículas completadas) | 17 | 230 |
| Final | cobrados = completadas = cupos consumidos | 13 = 13 = 13 | 15 = 15 = 15 |

Cómo leerlo:

- **Con patrones** el circuito abre a los ~2 s del inicio de la falla (los primeros cobros agotan el timeout) y el bulkhead
  rechazó 56 matrículas mientras tanto (`bulkhead_in_flight` nunca pasó de 5). Desde entonces el 99 % de las respuestas
  salen en < 250 ms como `503` + `Retry-After`; 555 llamadas se cortaron sin tocar a Pagos. El resto del sistema (sondas)
  no notó la falla. Al volver Pagos el circuito pasó por half-open y cerró.
- **Sin patrones** cada matrícula espera su timeout (y otro para el reembolso), el pool de hilos del orquestador se
  llena, y las sondas, que no tocan Pagos, se vuelven lentas y fallan: la cascada. Además, las sagas colgadas retienen
  sus reservas de cupo hasta que el worker las compensa (230 cupos ocupados vs. 17), de modo que alumnos legítimos
  podrían quedar sin cupo, y Pagos recibió cientos de llamadas inútiles mientras estaba caído.
- En **ambos** modos el estado final es consistente: lo cobrado, lo inscrito y los cupos consumidos coinciden, porque
  los pasos y compensaciones son idempotentes. La resiliencia no cambia la corrección, cambia el costo de la falla.

Salvedades de la medición: (1) el contador "llegaron a Pagos" se incrementa al *terminar* cada petición, así que las
lentas se cuentan en la fase siguiente; comparar el total de las fases, no una sola. (2) La sonda del panel del BFF
tarda ~2 s con Pagos lento aun con patrones: el BFF espera su propio timeout de 2 s hacia Pagos (ADR-006, "costo
aceptado") y entrega el panel con la sección de pagos como no disponible; no se contagia a las otras secciones.
(3) Una sola máquina compartió CPU entre cliente y servicios.

## Costo aceptado

- Mientras el circuito está abierto no se puede matricular (la regla de negocio lo impide); el cliente recibe `Retry-After`.
- Un fallo de red transitorio puede abrir el circuito unos segundos aunque Pagos esté sano (por eso el umbral pide
  5 llamadas y 50 %).
- El estado del circuito y del bulkhead vive en memoria del proceso: sirve para una sola instancia del orquestador
  (igual que el worker). Con varias réplicas cada una tendría su propio circuito.
- Cuando se rechaza una matrícula por bulkhead o circuito, el cupo se reserva y se libera de inmediato (dos llamadas
  gRPC de milisegundos). Evitarlo exigiría consultar el circuito antes del paso 1; se descartó por no tocar la Saga.
- El BFF no usa circuit breaker hacia Pagos (mejora opcional).

## Consecuencias

Los clientes deben tratar `503` con `Retry-After` como "reintente más tarde" (con la misma `Idempotency-Key` si la
liberaron). Las métricas `circuit_state`, `bulkhead_in_flight`, `bulkhead_rejected_total`, `circuit_rejected_total`,
`pagos_llamadas_total{operacion,resultado}` y `pagos_reintentos_total` alimentan el dashboard de Grafana y son la
evidencia del escenario 3.
