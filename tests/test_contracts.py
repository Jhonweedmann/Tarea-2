from pathlib import Path
import unittest

import yaml

from scripts.generar_stubs import generar

generar()
from cupos.app import cupos_pb2  # noqa: E402


ROOT = Path(__file__).parents[1]


class ContractTests(unittest.TestCase):
    def test_grpc_contract_exposes_required_operations(self):
        service = cupos_pb2.DESCRIPTOR.services_by_name["Cupos"]
        self.assertEqual(
            {method.name for method in service.methods},
            {"ConsultarCurso", "ListarCursos", "OcuparCupo", "LiberarCupo", "ReservarCupo", "LiberarReserva"},
        )
        self.assertTrue(service.methods_by_name["ListarCursos"].server_streaming)

    def test_openapi_de_inscripcion_es_interno_y_usa_problem_json(self):
        contract = yaml.safe_load((ROOT / "inscripcion" / "openapi.yaml").read_text(encoding="utf-8"))
        # La autenticación es del gateway: el servicio interno no declara esquemas de seguridad.
        self.assertNotIn("security", contract)
        self.assertNotIn("securitySchemes", contract["components"])
        self.assertIn("/estudiantes", contract["paths"])
        self.assertFalse([ruta for ruta in contract["paths"] if ruta.startswith("/v1")])
        for nombre, respuesta in contract["components"]["responses"].items():
            self.assertEqual(respuesta["content"].keys(), {"application/problem+json"}, nombre)
