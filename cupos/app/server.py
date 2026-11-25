import logging
import os
import threading
from concurrent import futures
from datetime import datetime, timezone

import grpc

from . import cupos_pb2, cupos_pb2_grpc
from .database import connect, seed
from .logs import registrar


class CuposService(cupos_pb2_grpc.CuposServicer):
    def __init__(self, database_path: str):
        self.db = connect(database_path)
        seed(self.db)
        # Todos los hilos de gRPC comparten esta conexion SQLite, que no admite uso simultaneo:
        # lecturas y escrituras pasan por el lock. Es reentrante porque OcuparCupo y LiberarCupo
        # llaman a ConsultarCurso mientras ya lo tienen tomado.
        self.lock = threading.RLock()

    @staticmethod
    def _curso(row):
        return cupos_pb2.Curso(
            id=row["id"], nombre=row["nombre"], cupos_totales=row["cupos_totales"],
            cupos_disponibles=row["cupos_disponibles"],
        )

    def ConsultarCurso(self, request, context):
        with self.lock:
            row = self.db.execute("SELECT * FROM cursos WHERE id = ?", (request.curso_id,)).fetchone()
        if not row:
            context.abort(grpc.StatusCode.NOT_FOUND, "Curso no encontrado")
        return self._curso(row)

    def ListarCursos(self, request, context):
        # Se leen todas las filas bajo el lock y se envian despues, para no retenerlo mientras el
        # stream espera al cliente.
        with self.lock:
            filas = self.db.execute("SELECT * FROM cursos ORDER BY id").fetchall()
        for row in filas:
            yield self._curso(row)

    def OcuparCupo(self, request, context):
        with self.lock:
            row = self.db.execute("SELECT * FROM cursos WHERE id = ?", (request.curso_id,)).fetchone()
            if not row:
                context.abort(grpc.StatusCode.NOT_FOUND, "Curso no encontrado")
            if row["cupos_disponibles"] == 0:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "No hay cupos disponibles")
            self.db.execute("UPDATE cursos SET cupos_disponibles = cupos_disponibles - 1 WHERE id = ?", (request.curso_id,))
            self.db.commit()
            return self.ConsultarCurso(request, context)

    def LiberarCupo(self, request, context):
        with self.lock:
            row = self.db.execute("SELECT * FROM cursos WHERE id = ?", (request.curso_id,)).fetchone()
            if not row:
                context.abort(grpc.StatusCode.NOT_FOUND, "Curso no encontrado")
            if row["cupos_disponibles"] >= row["cupos_totales"]:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Todos los cupos ya estan liberados")
            self.db.execute("UPDATE cursos SET cupos_disponibles = cupos_disponibles + 1 WHERE id = ?", (request.curso_id,))
            self.db.commit()
            return self.ConsultarCurso(request, context)

    # ------------------------------------------------------------------ Saga
    # ReservarCupo es el paso 1 de la Saga y LiberarReserva su compensacion.
    # Ambas son idempotentes por reserva_id: repetir la llamada devuelve lo
    # guardado sin volver a tocar los cupos del curso.

    def _buscar_reserva(self, reserva_id):
        return self.db.execute("SELECT * FROM reservas WHERE reserva_id = ?", (reserva_id,)).fetchone()

    def _reserva(self, row):
        curso = None
        if row["curso_id"]:
            fila_curso = self.db.execute("SELECT * FROM cursos WHERE id = ?", (row["curso_id"],)).fetchone()
            curso = self._curso(fila_curso)
        return cupos_pb2.Reserva(
            reserva_id=row["reserva_id"], curso_id=row["curso_id"] or "", estado=row["estado"], curso=curso,
        )

    @staticmethod
    def _rechazar(context, request, codigo, mensaje):
        registrar(context, "operacion rechazada", logging.WARNING, saga_id=request.reserva_id,
                  codigo=codigo.name, motivo=mensaje)
        context.abort(codigo, mensaje)

    def ReservarCupo(self, request, context):
        if not request.reserva_id:
            self._rechazar(context, request, grpc.StatusCode.INVALID_ARGUMENT, "reserva_id es obligatorio")
        with self.lock:
            existente = self._buscar_reserva(request.reserva_id)
            if existente:
                if existente["estado"] == "liberada":
                    # Lapida: la compensacion llego antes (timeout ambiguo). No se reserva.
                    self._rechazar(context, request, grpc.StatusCode.FAILED_PRECONDITION, "La reserva ya fue liberada")
                if existente["curso_id"] != request.curso_id:
                    self._rechazar(context, request, grpc.StatusCode.INVALID_ARGUMENT, "reserva_id ya usado para otro curso")
                registrar(context, "reserva repetida, no se descuenta", saga_id=request.reserva_id)
                return self._reserva(existente)  # repeticion: no se descuenta otra vez

            curso = self.db.execute("SELECT * FROM cursos WHERE id = ?", (request.curso_id,)).fetchone()
            if not curso:
                self._rechazar(context, request, grpc.StatusCode.NOT_FOUND, "Curso no encontrado")
            if curso["cupos_disponibles"] == 0:
                self._rechazar(context, request, grpc.StatusCode.FAILED_PRECONDITION, "No hay cupos disponibles")

            ahora = _ahora()
            # Descontar el cupo y registrar la reserva en una sola transaccion:
            # nunca queda un cupo descontado sin su reserva, ni al reves.
            with self.db:
                self.db.execute(
                    "UPDATE cursos SET cupos_disponibles = cupos_disponibles - 1 WHERE id = ?", (request.curso_id,)
                )
                self.db.execute(
                    "INSERT INTO reservas VALUES (?, ?, 'reservada', ?, ?)",
                    (request.reserva_id, request.curso_id, ahora, ahora),
                )
            registrar(context, "cupo reservado", saga_id=request.reserva_id, curso_id=request.curso_id,
                      cupos_disponibles=curso["cupos_disponibles"] - 1)
            return self._reserva(self._buscar_reserva(request.reserva_id))

    def LiberarReserva(self, request, context):
        if not request.reserva_id:
            self._rechazar(context, request, grpc.StatusCode.INVALID_ARGUMENT, "reserva_id es obligatorio")
        with self.lock:
            existente = self._buscar_reserva(request.reserva_id)
            if not existente:
                # La reserva nunca se aplico: no hay nada que devolver, pero se guarda
                # una lapida para rechazar un ReservarCupo atrasado con este id.
                ahora = _ahora()
                with self.db:
                    self.db.execute(
                        "INSERT INTO reservas VALUES (?, NULL, 'liberada', ?, ?)", (request.reserva_id, ahora, ahora)
                    )
                registrar(context, "liberar sin reserva previa: lapida", saga_id=request.reserva_id)
            elif existente["estado"] == "reservada":
                with self.db:
                    self.db.execute(
                        "UPDATE cursos SET cupos_disponibles = cupos_disponibles + 1 WHERE id = ?",
                        (existente["curso_id"],),
                    )
                    self.db.execute(
                        "UPDATE reservas SET estado = 'liberada', actualizada_en = ? WHERE reserva_id = ?",
                        (_ahora(), request.reserva_id),
                    )
                registrar(context, "reserva liberada", saga_id=request.reserva_id, curso_id=existente["curso_id"])
            else:
                # Ya estaba liberada: no se hace nada, la compensacion es idempotente.
                registrar(context, "liberacion repetida, no se suma", saga_id=request.reserva_id)
            return self._reserva(self._buscar_reserva(request.reserva_id))


def _ahora():
    return datetime.now(timezone.utc).isoformat()


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    cupos_pb2_grpc.add_CuposServicer_to_server(
        CuposService(os.getenv("CUPOS_DB", "/data/cupos.db")), server
    )
    server.add_insecure_port("[::]:" + os.getenv("GRPC_PORT", "50051"))
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
