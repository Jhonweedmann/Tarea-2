"""Pruebas de contrato del servicio de Inscripción: la API real contra inscripcion/openapi.yaml."""
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from inscripcion.app import main

SPEC = yaml.safe_load((Path(__file__).parents[1] / "inscripcion" / "openapi.yaml").read_text(encoding="utf-8"))


class ApiContratoTests(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        main.DB_PATH = f"{self.directorio.name}/inscripcion.db"
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directorio.cleanup()

    def cumple_contrato(self, respuesta, ruta, metodo):
        """Verifica que el código esté declarado y que el cuerpo cumpla el esquema del contrato."""
        declaradas = SPEC["paths"][ruta][metodo]["responses"]
        codigo = str(respuesta.status_code)
        self.assertIn(codigo, declaradas, f"{metodo.upper()} {ruta} devolvió {codigo}, no declarado: {respuesta.text}")
        declarada = declaradas[codigo]
        if "$ref" in declarada:
            declarada = SPEC["components"]["responses"][declarada["$ref"].rsplit("/", 1)[-1]]
        tipo = respuesta.headers["content-type"].split(";")[0]
        self.assertIn(tipo, declarada["content"])
        Draft202012Validator({**SPEC, **declarada["content"][tipo]["schema"]}).validate(respuesta.json())
        for cabecera in declarada.get("headers", {}):
            self.assertIn(cabecera, respuesta.headers)
        return respuesta.json()

    def crear_estudiante(self, email="ana@example.com"):
        r = self.client.post("/estudiantes", json={"nombre": "Ana Perez", "email": email})
        return self.cumple_contrato(r, "/estudiantes", "post")

    # --- Sin autenticación propia: la hace el gateway ---

    def test_no_exige_token(self):
        r = self.client.get("/estudiantes")
        self.assertEqual(self.cumple_contrato(r, "/estudiantes", "get")["items"], [])

    # --- Estudiantes y colecciones ---

    def test_crear_estudiante_devuelve_location_interna_y_rechaza_correo_repetido(self):
        r = self.client.post("/estudiantes", json={"nombre": "Ana Perez", "email": "ana@example.com"})
        estudiante = self.cumple_contrato(r, "/estudiantes", "post")
        self.assertEqual(r.headers["location"], f"/estudiantes/{estudiante['id']}")
        r = self.client.get(f"/estudiantes/{estudiante['id']}")
        self.assertEqual(self.cumple_contrato(r, "/estudiantes/{estudiante_id}", "get"), estudiante)
        r = self.client.post("/estudiantes", json={"nombre": "Otra", "email": "ana@example.com"})
        self.cumple_contrato(r, "/estudiantes", "post")
        self.assertEqual(r.status_code, 409)

    def test_estudiante_inexistente_responde_404(self):
        r = self.client.get("/estudiantes/no-existe")
        self.cumple_contrato(r, "/estudiantes/{estudiante_id}", "get")
        self.assertEqual(r.status_code, 404)

    def test_listado_paginado(self):
        for i in range(3):
            self.crear_estudiante(f"e{i}@example.com")
        r = self.client.get("/estudiantes?limit=2&offset=1")
        pagina = self.cumple_contrato(r, "/estudiantes", "get")
        self.assertEqual((len(pagina["items"]), pagina["total"]), (2, 3))
        r = self.client.get("/estudiantes?limit=0")
        self.assertEqual(self.cumple_contrato(r, "/estudiantes", "get")["status"], 422)

    def test_cuerpo_invalido_responde_422_problem_json(self):
        r = self.client.post("/estudiantes", json={"nombre": "A"})
        cuerpo = self.cumple_contrato(r, "/estudiantes", "post")
        self.assertEqual(r.status_code, 422)
        self.assertTrue(cuerpo["errors"])

    # --- Inscripciones: paso 3 de la Saga y su compensación ---

    def inscribir(self, inscripcion_id, estudiante_id, curso_id="ARQ-101"):
        r = self.client.post("/inscripciones", json={"inscripcion_id": inscripcion_id,
                                                     "estudiante_id": estudiante_id, "curso_id": curso_id})
        return r, self.cumple_contrato(r, "/inscripciones", "post")

    def test_inscribir_repetido_no_crea_otra(self):
        estudiante = self.crear_estudiante()
        r1, primera = self.inscribir("saga-1", estudiante["id"])
        r2, repetida = self.inscribir("saga-1", estudiante["id"])
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(r1.headers["location"], "/inscripciones/saga-1")
        self.assertEqual(primera, repetida)
        self.assertEqual(primera["estado"], "activa")
        r = self.client.get("/inscripciones", params={"estudiante_id": estudiante["id"]})
        self.assertEqual(len(self.cumple_contrato(r, "/inscripciones", "get")["items"]), 1)

    def test_inscribir_estudiante_inexistente_responde_404(self):
        r, _ = self.inscribir("saga-1", "no-existe")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get("/inscripciones/saga-1").status_code, 404)

    def test_segunda_inscripcion_activa_en_el_mismo_curso_responde_409(self):
        estudiante = self.crear_estudiante()
        self.inscribir("saga-1", estudiante["id"])
        r, _ = self.inscribir("saga-2", estudiante["id"])
        self.assertEqual(r.status_code, 409)
        # Tras dar de baja la primera, el estudiante puede volver a inscribirse.
        self.client.post("/inscripciones/saga-1/baja")
        r, _ = self.inscribir("saga-3", estudiante["id"])
        self.assertEqual(r.status_code, 201)

    def test_mismo_id_con_otros_datos_responde_409(self):
        estudiante = self.crear_estudiante()
        self.inscribir("saga-1", estudiante["id"], "ARQ-101")
        r, _ = self.inscribir("saga-1", estudiante["id"], "API-201")
        self.assertEqual(r.status_code, 409)

    def test_baja_es_idempotente(self):
        estudiante = self.crear_estudiante()
        self.inscribir("saga-1", estudiante["id"])
        for _ in range(2):
            r = self.client.post("/inscripciones/saga-1/baja")
            baja = self.cumple_contrato(r, "/inscripciones/{inscripcion_id}/baja", "post")
            self.assertEqual((r.status_code, baja["estado"]), (200, "baja"))
            self.assertEqual(baja["estudiante_id"], estudiante["id"])
        r = self.client.get("/inscripciones", params={"estudiante_id": estudiante["id"]})
        self.assertEqual([i["estado"] for i in r.json()["items"]], ["baja"])

    def test_baja_de_inexistente_deja_lapida_y_rechaza_inscripcion_atrasada(self):
        estudiante = self.crear_estudiante()
        r = self.client.post("/inscripciones/saga-1/baja")
        lapida = self.cumple_contrato(r, "/inscripciones/{inscripcion_id}/baja", "post")
        self.assertEqual((r.status_code, lapida["estado"]), (200, "baja"))
        self.assertNotIn("estudiante_id", lapida)
        # El paso 3 llega después de su compensación (timeout ambiguo): se rechaza.
        r, _ = self.inscribir("saga-1", estudiante["id"])
        self.assertEqual(r.status_code, 409)
        r = self.client.get("/inscripciones", params={"estudiante_id": estudiante["id"]})
        self.assertEqual(r.json()["items"], [])

    def test_listar_inscripciones_exige_estudiante_id(self):
        r = self.client.get("/inscripciones")
        self.cumple_contrato(r, "/inscripciones", "get")
        self.assertEqual(r.status_code, 422)

    # --- Inyección de fallas y observabilidad ---

    def test_admin_falla_hace_fallar_el_paso_3_sin_afectar_health(self):
        estudiante = self.crear_estudiante()
        try:
            r = self.client.post("/admin/falla", json={"caido": True})
            self.assertTrue(self.cumple_contrato(r, "/admin/falla", "post")["caido"])
            r, _ = self.inscribir("saga-1", estudiante["id"])
            self.assertEqual(r.status_code, 503)
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.get("/metrics").status_code, 200)
        finally:
            self.client.post("/admin/falla", json={"caido": False})
        r, _ = self.inscribir("saga-1", estudiante["id"])
        self.assertEqual(r.status_code, 201)

    def test_propaga_correlation_id_y_expone_metricas(self):
        r = self.client.get("/estudiantes", headers={"X-Correlation-Id": "demo-1"})
        self.assertEqual(r.headers["x-correlation-id"], "demo-1")
        metricas = self.client.get("/metrics").text
        self.assertIn('http_requests_total{route="/estudiantes",service="inscripcion",status="200"}', metricas)

    # --- Formato uniforme de errores ---

    def test_ruta_y_metodo_inexistentes_responden_problem_json(self):
        r = self.client.get("/no-existe")
        self.assertEqual((r.status_code, r.headers["content-type"]), (404, "application/problem+json"))
        r = self.client.delete("/estudiantes")
        self.assertEqual((r.status_code, r.headers["content-type"]), (405, "application/problem+json"))

    def test_las_rutas_v1_ya_no_existen(self):
        self.assertEqual(self.client.get("/v1/estudiantes").status_code, 404)

    def test_la_api_publica_el_contrato_escrito_a_mano(self):
        self.assertEqual(self.client.get("/openapi.json").json(), SPEC)


if __name__ == "__main__":
    unittest.main()
