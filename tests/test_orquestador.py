"""Pruebas del orquestador de la Saga, con Cupos, Pagos, Inscripción y Redis simulados."""
import tempfile
import unittest

import grpc
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from scripts.generar_stubs import generar

generar()
from orquestador.app import main  # noqa: E402
from orquestador.app.database import RepositorioSagas, connect  # noqa: E402
from orquestador.app.inscripcion_client import InscripcionError  # noqa: E402
from orquestador.app.pagos_client import PagosNoDisponible  # noqa: E402


# ---------------------------------------------------------------- Servicios simulados
# Cada uno anota en `registro` las llamadas que recibe, para verificar el orden de la Saga.

class FallaRpc(grpc.RpcError):
    def __init__(self, codigo, detalle=""):
        self._codigo, self._detalle = codigo, detalle

    def code(self):
        return self._codigo

    def details(self):
        return self._detalle


class CuposFalso:
    def __init__(self, registro):
        self.registro = registro
        self.disponibles = {"ARQ-101": 2, "API-201": 0}
        self.reservas = {}
        self.falla_reservar = self.falla_liberar = None

    def reservar(self, reserva_id, curso_id):
        self.registro.append("reservar_cupo")
        if self.falla_reservar:
            raise self.falla_reservar
        if reserva_id in self.reservas:
            return
        if curso_id not in self.disponibles:
            raise FallaRpc(grpc.StatusCode.NOT_FOUND, "Curso no encontrado")
        if self.disponibles[curso_id] == 0:
            raise FallaRpc(grpc.StatusCode.FAILED_PRECONDITION, "No hay cupos disponibles")
        self.disponibles[curso_id] -= 1
        self.reservas[reserva_id] = curso_id

    def liberar(self, reserva_id):
        self.registro.append("liberar_cupo")
        if self.falla_liberar:
            raise self.falla_liberar
        if (curso := self.reservas.pop(reserva_id, None)) is not None:
            self.disponibles[curso] += 1


class PagosFalso:
    def __init__(self, registro):
        self.registro = registro
        self.cobros = {}
        self.falla_cobrar = self.falla_reembolsar = None

    def cobrar(self, cobro_id, estudiante_id, curso_id, monto):
        self.registro.append("cobrar")
        if self.falla_cobrar:
            raise self.falla_cobrar
        self.cobros.setdefault(cobro_id, "cobrado")
        return {"cobro_id": cobro_id, "estado": self.cobros[cobro_id]}

    def reembolsar(self, cobro_id):
        self.registro.append("reembolsar")
        if self.falla_reembolsar:
            raise self.falla_reembolsar
        self.cobros[cobro_id] = "reembolsado"
        return {"cobro_id": cobro_id, "estado": "reembolsado"}

    def cobrados(self):
        return [cobro for cobro, estado in self.cobros.items() if estado == "cobrado"]


class InscripcionFalsa:
    def __init__(self, registro):
        self.registro = registro
        self.estudiantes = {"est-001"}
        self.inscripciones = {}
        self.falla_validar = self.falla_inscribir = self.falla_baja = None

    def estudiante_existe(self, estudiante_id):
        if self.falla_validar:
            raise self.falla_validar
        return estudiante_id in self.estudiantes

    def inscribir(self, inscripcion_id, estudiante_id, curso_id):
        self.registro.append("inscribir")
        if self.falla_inscribir:
            raise self.falla_inscribir
        if estudiante_id not in self.estudiantes:
            raise InscripcionError("Estudiante no encontrado", status=404)
        self.inscripciones.setdefault(inscripcion_id, "activa")
        return {"inscripcion_id": inscripcion_id, "estado": self.inscripciones[inscripcion_id]}

    def dar_baja(self, inscripcion_id):
        self.registro.append("dar_baja")
        if self.falla_baja:
            raise self.falla_baja
        self.inscripciones[inscripcion_id] = "baja"
        return {"inscripcion_id": inscripcion_id, "estado": "baja"}

    def activas(self):
        return [i for i, estado in self.inscripciones.items() if estado == "activa"]


def sagas_con_resultado(resultado):
    return REGISTRY.get_sample_value("saga_total", {"resultado": resultado}) or 0


class RepositorioTests(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.db = connect(f"{self.directorio.name}/orquestador.db")
        self.repo = RepositorioSagas(self.db)

    def tearDown(self):
        self.db.close()
        self.directorio.cleanup()

    def test_guarda_estado_y_pasos_en_orden(self):
        self.repo.crear("saga-1", "est-001", "ARQ-101", idempotency_key="k-1")
        self.repo.registrar_paso("saga-1", "reservar_cupo", "ok")
        self.repo.cambiar_estado("saga-1", "CUPO_RESERVADO")
        self.repo.registrar_paso("saga-1", "cobrar", "incierto", "timeout")
        saga = self.repo.obtener("saga-1")
        self.assertEqual((saga["estado"], saga["idempotency_key"]), ("CUPO_RESERVADO", "k-1"))
        self.assertEqual([(p["paso"], p["resultado"]) for p in self.repo.pasos("saga-1")],
                         [("reservar_cupo", "ok"), ("cobrar", "incierto")])

    def test_rechaza_estados_y_campos_desconocidos(self):
        self.repo.crear("saga-1", "est-001", "ARQ-101")
        with self.assertRaises(Exception):
            self.repo.cambiar_estado("saga-1", "INVENTADO")
        with self.assertRaises(ValueError):
            self.repo.cambiar_estado("saga-1", "COMPENSADA", estudiante_id="otro")


class ApiSagasTests(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        main.DB_PATH = f"{self.directorio.name}/orquestador.db"
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.repo = main.app.state.sagas

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directorio.cleanup()

    def test_saga_inexistente_responde_404_problem_json(self):
        r = self.client.get("/sagas/no-existe")
        self.assertEqual((r.status_code, r.headers["content-type"]), (404, "application/problem+json"))

    def test_representacion_con_pasos_y_enlace_publico(self):
        self.repo.crear("saga-1", "est-001", "ARQ-101")
        self.repo.registrar_paso("saga-1", "reservar_cupo", "ok")
        r = self.client.get("/sagas/saga-1", headers={"X-Forwarded-Prefix": "/api/v1"})
        saga = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(saga["estado"], "INICIADA")
        self.assertEqual([p["paso"] for p in saga["pasos"]], ["reservar_cupo"])
        self.assertNotIn("detalle", saga["pasos"][0])
        self.assertEqual(saga["_links"]["self"], {"href": "/api/v1/sagas/saga-1", "method": "GET"})

    def test_health_y_metricas(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/metrics").status_code, 200)


class SagaTests(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        main.DB_PATH = f"{self.directorio.name}/orquestador.db"
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.registro = []
        self.cupos = main.app.state.cupos = CuposFalso(self.registro)
        self.pagos = main.app.state.pagos = PagosFalso(self.registro)
        self.inscripcion = main.app.state.inscripcion = InscripcionFalsa(self.registro)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directorio.cleanup()

    def matricular(self, estudiante_id="est-001", curso_id="ARQ-101", headers=None):
        return self.client.post("/matriculas", json={"estudiante_id": estudiante_id, "curso_id": curso_id},
                                headers=headers or {})

    def pasos(self, saga_id):
        return [(p["paso"], p["resultado"]) for p in self.client.get(f"/sagas/{saga_id}").json()["pasos"]]

    # --- Escenario 1: camino feliz ---

    def test_camino_feliz_deja_los_tres_pasos_firmes(self):
        antes = sagas_con_resultado("COMPLETADA")
        r = self.matricular(headers={"X-Forwarded-Prefix": "/api/v1"})
        saga = r.json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.headers["location"], f"/sagas/{saga['saga_id']}")
        self.assertEqual(saga["_links"]["self"]["href"], f"/api/v1/sagas/{saga['saga_id']}")
        self.assertEqual(saga["estado"], "COMPLETADA")
        self.assertEqual(self.registro, ["reservar_cupo", "cobrar", "inscribir"])
        self.assertEqual(self.pasos(saga["saga_id"]), [("validar_estudiante", "ok"), ("reservar_cupo", "ok"),
                                                       ("cobrar", "ok"), ("inscribir", "ok")])
        # El saga_id es la clave idempotente de los tres servicios.
        self.assertEqual(self.cupos.reservas, {saga["saga_id"]: "ARQ-101"})
        self.assertEqual(self.pagos.cobrados(), [saga["saga_id"]])
        self.assertEqual(self.inscripcion.activas(), [saga["saga_id"]])
        self.assertEqual(sagas_con_resultado("COMPLETADA"), antes + 1)

    # --- Escenario 2: falla el último paso y se compensa en orden inverso ---

    def test_falla_en_paso_3_reembolsa_y_luego_libera_el_cupo(self):
        antes = sagas_con_resultado("COMPENSADA")
        self.inscripcion.falla_inscribir = InscripcionError("Inscripción respondió 503", status=503)
        r = self.matricular()
        cuerpo = r.json()
        self.assertEqual((r.status_code, r.headers["content-type"]), (502, "application/problem+json"))
        self.assertEqual(cuerpo["estado"], "COMPENSADA")
        self.assertEqual(self.registro, ["reservar_cupo", "cobrar", "inscribir", "reembolsar", "liberar_cupo"])
        self.assertEqual(self.pasos(cuerpo["saga_id"])[-3:],
                         [("inscribir", "error"), ("reembolsar", "ok"), ("liberar_cupo", "ok")])
        # Sin estado inconsistente: ni cobro sin inscripción, ni reserva colgada.
        self.assertEqual((self.pagos.cobrados(), self.cupos.reservas, self.inscripcion.activas()), ([], {}, []))
        self.assertEqual(self.cupos.disponibles["ARQ-101"], 2)
        self.assertEqual(sagas_con_resultado("COMPENSADA"), antes + 1)

    def test_paso_3_incierto_tambien_se_da_de_baja(self):
        self.inscripcion.falla_inscribir = InscripcionError("Inscripción no respondió", incierta=True)
        r = self.matricular()
        self.assertEqual((r.status_code, r.json()["estado"]), (502, "COMPENSADA"))
        self.assertEqual(self.registro[2:], ["inscribir", "dar_baja", "reembolsar", "liberar_cupo"])

    def test_inscripcion_rechazada_por_negocio_responde_409_y_compensa(self):
        self.inscripcion.falla_inscribir = InscripcionError("Inscripción duplicada", status=409)
        r = self.matricular()
        self.assertEqual((r.status_code, r.json()["estado"]), (409, "COMPENSADA"))
        self.assertEqual(self.pagos.cobrados(), [])

    # --- Fallas antes del cobro ---

    def test_sin_cupo_falla_en_paso_1_sin_cobrar(self):
        antes = sagas_con_resultado("ABORTADA_SIN_CUPO")
        r = self.matricular(curso_id="API-201")
        self.assertEqual((r.status_code, r.json()["estado"]), (409, "ABORTADA_SIN_CUPO"))
        self.assertEqual(self.registro, ["reservar_cupo"])
        self.assertEqual(self.pagos.cobros, {})
        self.assertEqual(sagas_con_resultado("ABORTADA_SIN_CUPO"), antes + 1)

    def test_estudiante_o_curso_inexistente_responde_404_sin_tocar_nada(self):
        r = self.matricular(estudiante_id="no-existe")
        self.assertEqual((r.status_code, r.json()["estado"]), (404, "ABORTADA_NO_ENCONTRADO"))
        self.assertEqual(self.registro, [])
        r = self.matricular(curso_id="NOPE")
        self.assertEqual((r.status_code, r.json()["estado"]), (404, "ABORTADA_NO_ENCONTRADO"))
        self.assertEqual(self.registro, ["reservar_cupo"])

    def test_inscripcion_caida_en_la_validacion_no_bloquea_la_matricula(self):
        self.inscripcion.falla_validar = InscripcionError("Inscripción no disponible")
        r = self.matricular()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.pasos(r.json()["saga_id"])[0], ("validar_estudiante", "incierto"))

    def test_cupos_sin_respuesta_libera_la_reserva_por_si_acaso(self):
        self.cupos.falla_reservar = FallaRpc(grpc.StatusCode.DEADLINE_EXCEEDED)
        r = self.matricular()
        self.assertEqual((r.status_code, r.json()["estado"]), (503, "COMPENSADA"))
        self.assertIn("retry-after", r.headers)
        self.assertEqual(self.registro, ["reservar_cupo", "liberar_cupo"])

    # --- Pagos no disponible (dependencia frágil) ---

    def test_pagos_no_disponible_sin_enviar_solo_libera_el_cupo(self):
        self.pagos.falla_cobrar = PagosNoDisponible("circuito abierto", puede_haberse_aplicado=False, retry_after=7)
        r = self.matricular()
        self.assertEqual((r.status_code, r.headers["retry-after"]), (503, "7"))
        self.assertEqual(r.json()["estado"], "ABORTADA_PAGOS_NO_DISPONIBLE")
        self.assertEqual(self.registro, ["reservar_cupo", "cobrar", "liberar_cupo"])
        self.assertEqual(self.cupos.reservas, {})

    def test_pagos_con_timeout_reembolsa_por_si_acaso(self):
        self.pagos.falla_cobrar = PagosNoDisponible("timeout")
        r = self.matricular()
        self.assertEqual((r.status_code, r.json()["estado"]), (503, "ABORTADA_PAGOS_NO_DISPONIBLE"))
        self.assertEqual(self.registro, ["reservar_cupo", "cobrar", "reembolsar", "liberar_cupo"])

    # --- Compensación que falla ---

    def test_compensacion_fallida_queda_pendiente_y_respeta_el_orden(self):
        self.inscripcion.falla_inscribir = InscripcionError("Inscripción respondió 503", status=503)
        self.pagos.falla_reembolsar = PagosNoDisponible("timeout")
        r = self.matricular()
        saga_id = r.json()["saga_id"]
        self.assertEqual(r.json()["estado"], "COMPENSACION_PENDIENTE")
        # No se libera el cupo antes de reembolsar: el orden inverso es estricto.
        self.assertNotIn("liberar_cupo", self.registro)
        self.assertEqual(self.cupos.reservas, {saga_id: "ARQ-101"})

        self.pagos.falla_reembolsar = None
        saga = main.nueva_saga(main.app)
        self.assertEqual(saga.compensaciones_pendientes(saga_id), ["cobrar", "reservar_cupo"])
        self.assertTrue(saga.compensar(saga_id))
        self.assertEqual(self.client.get(f"/sagas/{saga_id}").json()["estado"], "COMPENSADA")
        self.assertEqual((self.pagos.cobrados(), self.cupos.reservas), ([], {}))

    def test_cuerpo_invalido_responde_422(self):
        r = self.client.post("/matriculas", json={"estudiante_id": ""})
        self.assertEqual((r.status_code, r.headers["content-type"]), (422, "application/problem+json"))


if __name__ == "__main__":
    unittest.main()
