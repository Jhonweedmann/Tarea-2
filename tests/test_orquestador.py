"""Pruebas del orquestador de la Saga, con Cupos, Pagos, Inscripción y Redis simulados."""
import tempfile
import unittest

from fastapi.testclient import TestClient

from orquestador.app import main
from orquestador.app.database import RepositorioSagas, connect


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


if __name__ == "__main__":
    unittest.main()
