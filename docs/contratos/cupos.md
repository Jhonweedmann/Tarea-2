# Cupos — gRPC (dueño: B)

`cupos:50051`, `package cupos.v1`. Contrato: [`cupos/cupos.proto`](../../cupos/cupos.proto).

## Se conservan (los usa el BFF)

- `ConsultarCurso(ConsultarCursoRequest) returns (Curso)` — `NOT_FOUND` si no existe.
- `ListarCursos(ListarCursosRequest) returns (stream Curso)`.

El BFF ya los consume. **No cambiar sus números de campo** (ADR-003: evolución aditiva).

## Se agregan (los usa el orquestador, pasos 1 y compensación)

Propuesta acordada para que B la implemente:

```proto
rpc ReservarCupo(ReservarCupoRequest) returns (Reserva);
rpc LiberarReserva(LiberarReservaRequest) returns (Reserva);

message ReservarCupoRequest  { string reserva_id = 1; string curso_id = 2; }
message LiberarReservaRequest { string reserva_id = 1; }
message Reserva {
  string reserva_id = 1;
  string curso_id = 2;
  string estado = 3;        // "reservada" | "liberada"
  Curso curso = 4;          // estado del curso después de la operación
}
```

| Caso | Resultado |
|---|---|
| `ReservarCupo` nuevo con cupo | descuenta 1, guarda `reservas(reserva_id PK, curso_id, estado)` |
| `ReservarCupo` repetido (mismo `reserva_id`) | devuelve la reserva guardada sin descontar otra vez |
| Sin cupo | `FAILED_PRECONDITION` |
| Curso no existe | `NOT_FOUND` |
| `ReservarCupo` sobre una reserva ya liberada | `FAILED_PRECONDITION` (lápida) |
| `LiberarReserva` de una reserva activa | suma 1, estado `liberada` |
| `LiberarReserva` repetido o de un id que no existe | OK no-op (guarda lápida si no existía) |

Se mantienen el `threading.Lock` y el `CHECK` contra sobrecupo de la Tarea 1.

`OcuparCupo` / `LiberarCupo` (sin id idempotente) quedan obsoletos; el cliente Node de la Tarea 1
(`clients/node-grpc-client`) todavía los usa, así que B decide si los conserva o actualiza ese cliente.

## Metadata

El llamador envía `x-correlation-id` y `x-client-id` (`comun.observabilidad.metadata_grpc()`).
Cupos puede leerla con `dict(context.invocation_metadata())` para sus logs.
