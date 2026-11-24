# ADR-007 · Matrícula como Saga orquestada (y no 2PC ni coreografía)

**Estado:** aceptada

## Contexto

Matricular a un alumno cruza tres servicios, cada uno con **su propia base de datos** (ADR-001):
Cupos reserva un cupo, Pagos cobra por una pasarela **externa** y Inscripción registra al alumno.
No existe una transacción que abarque las tres BD, y si una parte se confirma y otra no queda un
descuadre: cobro sin inscripción, inscripción sin cobro o un cupo reservado para nadie. Las reglas
de negocio son: no matricular por sobre el cupo, no inscribir sin cobro y que **reintentar no
cobre dos veces**. Además Pagos es la dependencia frágil: se pone lenta o se cae en los periodos de
matrícula (escenario 3, ADR-008).

## Alternativas consideradas

**Cómo lograr atomicidad entre servicios:**

- **Two-Phase Commit (2PC / XA):** un coordinador pide a todos "prepárate" (cada uno bloquea sus
  recursos) y luego "confirma" o "aborta". Da atomicidad real, pero: (1) Pagos es una pasarela
  externa que no participa en un coordinador XA ni ofrece un estado "preparado"; (2) mientras
  Pagos responde lento, el cupo queda **bloqueado** en Cupos para todos los demás alumnos; (3) si
  el coordinador cae entre las dos fases, los participantes quedan bloqueados sin saber qué
  hacer; (4) la disponibilidad de la matrícula pasa a ser el producto de la de los tres servicios
  y SQLite no soporta XA. Es justo lo contrario de lo que pide el escenario 3.
- **Saga:** cada paso es una transacción **local** que se confirma de inmediato, y cada uno tiene
  una **compensación** que lo deshace. Si un paso falla se compensan los anteriores en orden
  inverso. No hay bloqueos entre servicios; a cambio la consistencia es **eventual**: durante un
  momento puede existir un cobro sin inscripción, hasta que el reembolso lo corrige.

**Cómo coordinar la Saga:**

- **Coreografía:** no hay coordinador; cada servicio publica eventos (`CupoReservado`,
  `PagoCobrado`, `InscripcionFallida`...) en un broker y el siguiente reacciona. Desacopla a los
  servicios, pero el flujo y sobre todo las compensaciones quedan **repartidos en tres
  servicios**, no hay un lugar donde consultar en qué va una matrícula, y exige agregar un broker
  (RabbitMQ o Kafka) con su patrón outbox para no perder eventos.
- **Orquestación:** un servicio (`orquestador/`) llama a cada paso en orden, guarda el estado en
  su BD y decide cuándo y qué compensar.

## Decisión

1. **Saga orquestada** en un servicio propio, `orquestador/`, con su BD (`sagas` y `pasos_saga`).
   `POST /matriculas` ejecuta la saga de forma **síncrona** y responde con el resultado;
   `GET /sagas/{id}` muestra el historial.
2. **Orden de los pasos:** reservar cupo → cobrar → inscribir. El cupo va primero porque es el
   recurso escaso y el más barato de deshacer: si no hay cupo no se cobra. Inscribir va al final
   porque la regla prohíbe inscribir sin cobro.
3. **El `saga_id` es la clave idempotente de cada paso** (`reserva_id`, `cobro_id`,
   `inscripcion_id`). Repetir un paso o una compensación devuelve lo ya guardado; compensar algo
   que nunca llegó a existir es un no-op que deja una **lápida** para rechazar el paso si llega
   atrasado.
4. **Cada falla se clasifica** como `error` (seguro que no se aplicó: 4xx, 503, no conectó) o
   `incierto` (pudo aplicarse: timeout de lectura, conexión cortada, otro 5xx). Se compensan en
   orden inverso los pasos `ok` **e** `incierto`; los `error` no dejaron nada que deshacer.
5. **Si una compensación falla**, la saga queda en `COMPENSACION_PENDIENTE` sin saltarse el orden,
   y un **worker** la reintenta con backoff exponencial (1, 2, 4... s, tope 60 s). El mismo
   worker retoma las sagas que quedaron a medias si el orquestador se cae.
6. **`Idempotency-Key`** en `POST /matriculas` guardada en Redis con `SET NX` (como en la Tarea 1):
   la misma clave sobre una saga completada devuelve la misma respuesta sin volver a cobrar.

Los diagramas del flujo feliz y de las fallas están en [docs/diagramas/saga.md](../diagramas/saga.md).

## Justificación

- **Saga y no 2PC:** con una pasarela externa el 2PC no es implementable, y aunque lo fuera
  convertiría la lentitud de Pagos en bloqueos de cupos para todos. La Saga cambia atomicidad
  inmediata por consistencia eventual con compensaciones, que es lo que hace un negocio real: si
  no se pudo inscribir, se reembolsa.
- **Orquestación y no coreografía:** son tres pasos lineales con compensación en orden inverso. Con
  un orquestador el orden está escrito en un solo lugar (`saga.py`, tupla `PASOS`), el estado de
  cada matrícula se puede consultar y **es la evidencia** de los escenarios 1 y 2, y no hace falta
  un broker. El acoplamiento que se acepta (el orquestador conoce a los tres servicios) es bajo
  porque solo depende de sus contratos (`docs/contratos/`).
- **Síncrona y no 202 + consulta:** la secretaría necesita saber en el momento si el alumno quedó
  matriculado, y la saga completa toma decenas de milisegundos (medido abajo). Una saga asíncrona
  (`202 Accepted` y consultar `GET /sagas/{id}`) aguantaría mejor una Pagos muy lenta, pero sería
  más difícil de usar y de demostrar. Se acota el peor caso con timeouts de 2 s por llamada.
- **Compensar lo incierto:** ante un timeout no se sabe si Pagos cobró. Compensar de más es
  seguro (el reembolso de algo que no existe es un no-op con lápida); compensar de menos deja un
  cobro huérfano. Por eso el cliente de Pagos distingue `puede_haberse_aplicado`: con el circuito
  abierto la solicitud nunca salió y basta con liberar el cupo.

**Evidencia** (Docker, `python scripts/escenarios.py <escenario>`, ver README):

| Escenario | Resultado | Tiempos observados |
|---|---|---|
| 1 · éxito | `201 COMPLETADA`, inscripción activa, cobro `cobrado`, un cupo menos | saga completa en ~80 ms |
| 2 · falla el paso 3 (Inscripción caída) | `502 COMPENSADA`: `inscribir error` → `reembolsar ok` → `liberar_cupo ok` | cobro sin inscripción durante ~27 ms |
| Misma `Idempotency-Key` dos veces | `201` y luego `200 Idempotent-Replay`, misma saga | un solo cupo y un solo cobro |
| Sin cupo | `409 ABORTADA_SIN_CUPO` en el paso 1 | no se llama a Pagos |
| Compensación que falla (Inscripción lenta) | `COMPENSACION_PENDIENTE` → el worker termina → `COMPENSADA` | descuadre temporal de ~8 s, lo que duró la falla |

## Costo aceptado

- **Consistencia eventual:** hay una ventana en que existe un cobro sin inscripción. Normalmente
  son milisegundos, pero si la compensación falla dura lo que dure la caída del servicio (8 s en
  la prueba). Durante esa ventana el alumno podría ver un cobro que luego se reembolsa.
- **Compensaciones visibles:** un reembolso no "borra" el cobro; en una pasarela real puede tener
  costo y el alumno lo ve en su cartola. 2PC lo evitaría, pero no es aplicable.
- **Compensar de más:** ante la duda se compensa, y a veces se compensa algo que nunca ocurrió
  (queda una lápida). Es el precio de no dejar cobros huérfanos.
- **El orquestador es un punto central:** si se cae, no se matricula. Lo ya hecho no se pierde
  (está en su BD) y el worker lo deshace al volver, pero hoy se supone **una sola instancia**.
- **Saga síncrona:** cada matrícula ocupa un hilo del orquestador hasta ~14 s en el peor caso
  (siete llamadas de 2 s). Por eso el gateway espera 15 s en esta ruta y el bulkhead de C limita
  cuántas pueden quedar esperando a Pagos.

## Consecuencias

- Inscripción, Cupos y Pagos deben ser idempotentes por id y guardar lápidas (hecho en Cupos e
  Inscripción; contrato de Pagos en `docs/contratos/pagos.md`).
- El cliente de Pagos de C debe respetar la interfaz (`PagosNoDisponible.puede_haberse_aplicado`,
  `retry_after`, `PagosRechazado`) para que la Saga sepa si reembolsar.
- Si se agregan reintentos hacia Pagos (C), el peor caso de la saga crece: hay que revisar
  `GATEWAY_TIMEOUT_SAGA_SECONDS`.
- Para varias instancias del orquestador habría que repartir el trabajo del worker (por ejemplo
  `SELECT ... FOR UPDATE SKIP LOCKED` en PostgreSQL o un lock en Redis).
- Si el flujo creciera a muchos pasos de equipos distintos, o hubiera que matricular de forma
  masiva, convendría revisar la decisión: saga asíncrona o coreografía con broker.
