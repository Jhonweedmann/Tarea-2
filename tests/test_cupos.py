import tempfile
import unittest

from scripts.generar_stubs import generar

generar()
from cupos.app import cupos_pb2  # noqa: E402
from cupos.app.server import CuposService  # noqa: E402


class Context:
    def abort(self, code, details):
        raise RuntimeError((code, details))


class CuposTests(unittest.TestCase):
    def test_ocupar_y_liberar_cupo(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            request = cupos_pb2.ModificarCupoRequest(curso_id="API-201")
            self.assertEqual(service.OcuparCupo(request, Context()).cupos_disponibles, 0)
            with self.assertRaisesRegex(RuntimeError, "FAILED_PRECONDITION"):
                service.OcuparCupo(request, Context())
            self.assertEqual(service.LiberarCupo(request, Context()).cupos_disponibles, 1)
            service.db.close()

    def test_reservar_repetido_no_descuenta_dos_veces(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            request = cupos_pb2.ReservarCupoRequest(reserva_id="saga-1", curso_id="ARQ-101")
            primera = service.ReservarCupo(request, Context())
            repetida = service.ReservarCupo(request, Context())
            self.assertEqual(primera.estado, "reservada")
            self.assertEqual(repetida, primera)
            self.assertEqual(repetida.curso.cupos_disponibles, 1)  # ARQ-101 parte con 2
            service.db.close()

    def test_reservar_sin_cupo_o_curso_inexistente(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            service.ReservarCupo(cupos_pb2.ReservarCupoRequest(reserva_id="saga-1", curso_id="API-201"), Context())
            with self.assertRaisesRegex(RuntimeError, "FAILED_PRECONDITION"):
                service.ReservarCupo(cupos_pb2.ReservarCupoRequest(reserva_id="saga-2", curso_id="API-201"), Context())
            with self.assertRaisesRegex(RuntimeError, "NOT_FOUND"):
                service.ReservarCupo(cupos_pb2.ReservarCupoRequest(reserva_id="saga-3", curso_id="NOPE"), Context())
            # Las reservas rechazadas no quedan guardadas.
            self.assertEqual(service.db.execute("SELECT COUNT(*) FROM reservas").fetchone()[0], 1)
            service.db.close()

    def test_liberar_repetido_suma_una_sola_vez(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            service.ReservarCupo(cupos_pb2.ReservarCupoRequest(reserva_id="saga-1", curso_id="API-201"), Context())
            liberar = cupos_pb2.LiberarReservaRequest(reserva_id="saga-1")
            primera = service.LiberarReserva(liberar, Context())
            repetida = service.LiberarReserva(liberar, Context())
            self.assertEqual(primera.estado, "liberada")
            self.assertEqual(primera.curso.cupos_disponibles, 1)
            self.assertEqual(repetida.curso.cupos_disponibles, 1)  # no supera cupos_totales
            service.db.close()

    def test_liberar_inexistente_deja_lapida(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            lapida = service.LiberarReserva(cupos_pb2.LiberarReservaRequest(reserva_id="saga-1"), Context())
            self.assertEqual(lapida.estado, "liberada")
            # Un ReservarCupo atrasado con el mismo id (timeout ambiguo) se rechaza.
            with self.assertRaisesRegex(RuntimeError, "FAILED_PRECONDITION"):
                service.ReservarCupo(cupos_pb2.ReservarCupoRequest(reserva_id="saga-1", curso_id="ARQ-101"), Context())
            curso = service.ConsultarCurso(cupos_pb2.ConsultarCursoRequest(curso_id="ARQ-101"), Context())
            self.assertEqual(curso.cupos_disponibles, 2)
            service.db.close()

    def test_reservar_sin_reserva_id(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            with self.assertRaisesRegex(RuntimeError, "INVALID_ARGUMENT"):
                service.ReservarCupo(cupos_pb2.ReservarCupoRequest(curso_id="ARQ-101"), Context())
            service.db.close()

    def test_curso_inexistente(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CuposService(f"{directory}/cupos.db")
            with self.assertRaisesRegex(RuntimeError, "NOT_FOUND"):
                service.ConsultarCurso(cupos_pb2.ConsultarCursoRequest(curso_id="NOPE"), Context())
            service.db.close()
