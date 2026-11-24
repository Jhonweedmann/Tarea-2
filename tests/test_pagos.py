"""Pruebas del servicio Pagos: idempotencia por cobro_id, reembolso con lápida e inyección de fallas."""
import os
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

os.environ.setdefault("LOG_NIVEL", "CRITICAL")
from pagos.app import main  # noqa: E402

COBRO = {"cobro_id": "saga-1", "estudiante_id": "est-001", "curso_id": "ARQ-101", "monto": 150000}


class PagosTests(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        main.DB_PATH = f"{self.directorio.name}/pagos.db"
        main.SEMILLA = False
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.post("/admin/falla", json={"latencia_ms": 0, "caido": False, "tasa_error": 0})
        self.client.__exit__(None, None, None)
        self.directorio.cleanup()

    def cobros(self, estudiante="est-001"):
        return self.client.get("/cobros", params={"estudiante_id": estudiante}).json()["items"]

    def test_cobro_nuevo_responde_201(self):
        r = self.client.post("/cobros", json=COBRO)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["estado"], "cobrado")
        self.assertEqual(r.headers["location"], "/cobros/saga-1")

    def test_repetir_el_mismo_cobro_id_no_cobra_dos_veces(self):
        primero = self.client.post("/cobros", json=COBRO)
        segundo = self.client.post("/cobros", json=COBRO)
        self.assertEqual((primero.status_code, segundo.status_code), (201, 200))
        self.assertEqual(primero.json(), segundo.json())
        self.assertEqual(len(self.cobros()), 1)

    def test_reintentos_simultaneos_cobran_una_sola_vez(self):
        codigos = []
        hilos = [threading.Thread(target=lambda: codigos.append(self.client.post("/cobros", json=COBRO).status_code))
                 for _ in range(12)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        self.assertEqual(sorted(codigos), [200] * 11 + [201])
        self.assertEqual(len(self.cobros()), 1)

    def test_mismo_cobro_id_con_otros_datos_es_conflicto(self):
        self.client.post("/cobros", json=COBRO)
        r = self.client.post("/cobros", json={**COBRO, "monto": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.cobros()[0]["monto"], 150000)

    def test_reembolso_es_idempotente(self):
        self.client.post("/cobros", json=COBRO)
        primero = self.client.post("/cobros/saga-1/reembolso")
        segundo = self.client.post("/cobros/saga-1/reembolso")
        self.assertEqual((primero.status_code, segundo.status_code), (200, 200))
        self.assertEqual(segundo.json()["estado"], "reembolsado")
        self.assertEqual([c["estado"] for c in self.cobros()], ["reembolsado"])

    def test_cobro_ya_reembolsado_se_rechaza(self):
        self.client.post("/cobros", json=COBRO)
        self.client.post("/cobros/saga-1/reembolso")
        self.assertEqual(self.client.post("/cobros", json=COBRO).status_code, 409)

    def test_reembolso_de_un_cobro_inexistente_deja_lapida_y_rechaza_el_cobro_atrasado(self):
        r = self.client.post("/cobros/saga-9/reembolso")
        self.assertEqual((r.status_code, r.json()["estado"]), (200, "reembolsado"))
        atrasado = self.client.post("/cobros", json={**COBRO, "cobro_id": "saga-9"})
        self.assertEqual(atrasado.status_code, 409)
        self.assertEqual(self.cobros(), [])  # la lápida no aparece en el panel de ningún estudiante

    def test_listar_solo_los_cobros_del_estudiante(self):
        self.client.post("/cobros", json=COBRO)
        self.client.post("/cobros", json={**COBRO, "cobro_id": "saga-2", "estudiante_id": "est-002"})
        self.assertEqual([c["cobro_id"] for c in self.cobros("est-002")], ["saga-2"])

    def test_cuerpo_invalido_es_422_problem_json(self):
        r = self.client.post("/cobros", json={**COBRO, "monto": 0})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.headers["content-type"].split(";")[0], "application/problem+json")

    # --- Inyección de fallas ---

    def test_caido_responde_503_pero_health_y_admin_siguen_vivos(self):
        self.client.post("/admin/falla", json={"caido": True})
        self.assertEqual(self.client.post("/cobros", json=COBRO).status_code, 503)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertTrue(self.client.get("/admin/falla").json()["caido"])
        self.client.post("/admin/falla", json={"caido": False})
        self.assertEqual(self.client.post("/cobros", json=COBRO).status_code, 201)

    def test_tasa_error_1_responde_500(self):
        self.client.post("/admin/falla", json={"tasa_error": 1})
        self.assertEqual(self.client.post("/cobros", json=COBRO).status_code, 500)

    def test_una_falla_no_aplica_el_cobro(self):
        self.client.post("/admin/falla", json={"caido": True})
        self.client.post("/cobros", json=COBRO)
        self.client.post("/admin/falla", json={"caido": False})
        self.assertEqual(self.cobros(), [])


if __name__ == "__main__":
    unittest.main()
