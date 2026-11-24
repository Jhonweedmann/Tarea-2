# Diagramas de la Saga de matrícula

Decisión y justificación: [ADR-007](../adr/ADR-007-saga-orquestada.md). Código: `orquestador/app/saga.py`.
GitHub dibuja los bloques `mermaid` directamente; para exportarlos como imagen se puede usar
<https://mermaid.live>.

En todos los casos el `saga_id` viaja como clave idempotente (`reserva_id`, `cobro_id`,
`inscripcion_id`) y cada paso queda registrado en la BD del orquestador antes de seguir.

## 1. Flujo feliz (escenario 1)

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant G as Gateway
    participant O as Orquestador
    participant I as Inscripción
    participant CU as Cupos (gRPC)
    participant P as Pagos

    C->>G: POST /api/v1/matriculas (Bearer, Idempotency-Key)
    G->>O: POST /matriculas (X-Correlation-Id, X-Client-Id)
    O->>O: SET NX Idempotency-Key en Redis, crea saga INICIADA
    O->>I: GET /estudiantes/{id} (validación previa)
    I-->>O: 200
    O->>CU: ReservarCupo(reserva_id = saga_id)
    CU-->>O: reservada (descuenta 1 cupo)
    O->>O: estado CUPO_RESERVADO
    O->>P: POST /cobros (cobro_id = saga_id)
    P-->>O: 201 cobrado
    O->>O: estado PAGO_COBRADO
    O->>I: POST /inscripciones (inscripcion_id = saga_id)
    I-->>O: 201 activa
    O->>O: estado COMPLETADA, saga_total{COMPLETADA}++
    O-->>G: 201 + Location /sagas/{id}
    G-->>C: 201 + Location /api/v1/sagas/{id}
```

## 2. Falla el último paso y se compensa en orden inverso (escenario 2)

Inscripción está caída (`/admin/falla {"caido": true}` responde 503). Un 503 significa que la
operación seguro **no** se aplicó, así que no hace falta darla de baja: se deshacen los pasos 2 y 1.

```mermaid
sequenceDiagram
    autonumber
    participant O as Orquestador
    participant CU as Cupos
    participant P as Pagos
    participant I as Inscripción

    O->>CU: ReservarCupo(saga_id)
    CU-->>O: ok
    O->>P: POST /cobros (saga_id)
    P-->>O: 201 cobrado
    O->>I: POST /inscripciones (saga_id)
    I-->>O: 503 (no se aplicó)
    Note over O: paso "inscribir" = error, estado COMPENSANDO
    O->>P: POST /cobros/{saga_id}/reembolso
    P-->>O: 200 reembolsado
    O->>CU: LiberarReserva(saga_id)
    CU-->>O: liberada (suma 1 cupo)
    Note over O: estado COMPENSADA, responde 502 con saga_id
```

Si el paso 3 queda **incierto** (timeout), antes de reembolsar se llama también a
`POST /inscripciones/{saga_id}/baja`, que es idempotente y deja una lápida si la inscripción
nunca llegó.

## 3. Pagos no disponible (relación con el escenario 3)

```mermaid
sequenceDiagram
    autonumber
    participant O as Orquestador
    participant CU as Cupos
    participant P as Pagos (cliente de C)

    O->>CU: ReservarCupo(saga_id)
    CU-->>O: ok
    O->>P: cobrar(saga_id)
    alt circuito abierto o bulkhead lleno (la solicitud no salió)
        P-->>O: PagosNoDisponible(puede_haberse_aplicado = false)
        O->>CU: LiberarReserva(saga_id)
    else timeout (pudo haberse cobrado)
        P-->>O: PagosNoDisponible(puede_haberse_aplicado = true)
        O->>P: reembolsar(saga_id) por si acaso
        O->>CU: LiberarReserva(saga_id)
    end
    Note over O: estado ABORTADA_PAGOS_NO_DISPONIBLE, 503 + Retry-After
```

## 4. Una compensación falla y el worker la termina

```mermaid
sequenceDiagram
    autonumber
    participant O as Orquestador
    participant W as Worker
    participant I as Inscripción (lenta)
    participant P as Pagos
    participant CU as Cupos

    O->>I: POST /inscripciones (timeout 2 s)
    Note over O: inscribir = incierto
    O->>I: POST /inscripciones/{id}/baja (timeout 2 s)
    Note over O: dar_baja = error, estado COMPENSACION_PENDIENTE<br/>(no se salta el orden: aún no reembolsa)
    loop cada 2 s, con backoff 1, 2, 4... s
        W->>I: POST /inscripciones/{id}/baja
    end
    I-->>W: 200 baja
    W->>P: POST /cobros/{id}/reembolso
    W->>CU: LiberarReserva(id)
    Note over W: estado COMPENSADA
```

## 5. Estados de la saga

```mermaid
stateDiagram-v2
    [*] --> INICIADA
    INICIADA --> ABORTADA_NO_ENCONTRADO: estudiante o curso no existe
    INICIADA --> ABORTADA_SIN_CUPO: sin cupo (no se cobra)
    INICIADA --> CUPO_RESERVADO: reservar_cupo ok
    CUPO_RESERVADO --> PAGO_COBRADO: cobrar ok
    PAGO_COBRADO --> COMPLETADA: inscribir ok
    INICIADA --> COMPENSANDO: Cupos no responde
    CUPO_RESERVADO --> COMPENSANDO: cobrar falla
    PAGO_COBRADO --> COMPENSANDO: inscribir falla
    COMPENSANDO --> COMPENSADA: compensaciones ok
    COMPENSANDO --> ABORTADA_PAGOS_NO_DISPONIBLE: compensaciones ok (Pagos no disponible)
    COMPENSANDO --> COMPENSACION_PENDIENTE: una compensación falla
    COMPENSACION_PENDIENTE --> COMPENSADA: el worker termina
    COMPENSACION_PENDIENTE --> ABORTADA_PAGOS_NO_DISPONIBLE: el worker termina
    COMPLETADA --> [*]
    COMPENSADA --> [*]
    ABORTADA_SIN_CUPO --> [*]
    ABORTADA_NO_ENCONTRADO --> [*]
    ABORTADA_PAGOS_NO_DISPONIBLE --> [*]
```
