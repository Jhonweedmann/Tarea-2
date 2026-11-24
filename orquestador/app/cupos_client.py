"""Cliente gRPC de Cupos (paso 1 de la Saga y su compensación).

Los errores se dejan pasar como grpc.RpcError: la Saga decide qué significa cada código
(FAILED_PRECONDITION = sin cupo, NOT_FOUND = curso inexistente, el resto = incierto).
"""
import os

import grpc

from comun.observabilidad import metadata_grpc

from . import cupos_pb2, cupos_pb2_grpc


class CuposClient:
    """Un único canal reutilizable para todas las sagas."""

    def __init__(self, target: str | None = None, timeout: float | None = None):
        self.channel = grpc.insecure_channel(target or os.getenv("CUPOS_GRPC_TARGET", "localhost:50051"))
        self.stub = cupos_pb2_grpc.CuposStub(self.channel)
        # Deadline por llamada: Cupos lento no puede dejar la saga esperando indefinidamente.
        self.timeout = timeout or float(os.getenv("CUPOS_TIMEOUT_SECONDS", "2"))

    def reservar(self, reserva_id: str, curso_id: str):
        solicitud = cupos_pb2.ReservarCupoRequest(reserva_id=reserva_id, curso_id=curso_id)
        return self.stub.ReservarCupo(solicitud, timeout=self.timeout, metadata=metadata_grpc())

    def liberar(self, reserva_id: str):
        solicitud = cupos_pb2.LiberarReservaRequest(reserva_id=reserva_id)
        return self.stub.LiberarReserva(solicitud, timeout=self.timeout, metadata=metadata_grpc())

    def close(self) -> None:
        self.channel.close()
