"""Pruebas del BFF: agregación del panel y degradación parcial (Inscripción, Cupos y Pagos simulados)."""
import asyncio
import importlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path

import grpc
import httpx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from scripts.generar_stubs import generar

os.environ.setdefault("LOG_NIVEL", "CRITICAL")
generar()


def cargar_paquete(nombre: str, carpeta: Path):
    """La carpeta se llama bff-web (con guion), así que no se puede importar con `import`."""
    spec = importlib.util.spec_from_file_location(nombre, carpeta / "__init__.py",
                                                  submodule_search_locations=[str(carpeta)])
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)


cargar_paquete("bff_web", Path(__file__).parents[1] / "bff-web" / "app")
main = importlib.import_module("bff_web.main")
cupos_pb2 = importlib.import_module("bff_web.cupos_pb2")


class FallaRpc(grpc.RpcError):
    def __init__(self, codigo):
        self._codigo = codigo

    def code(self):
        return self._codigo


class CuposFalso:
    def __init__(self):
        self.falla = None
        self.cursos = [cupos_pb2.Curso(id="ARQ-101", nombre="Arquitectura de Sistemas", cupos_totales=2,
                                       cupos_disponibles=1),
                       cupos_pb2.Curso(id="API-201", nombre="Integracion de APIs", cupos_totales=1,
                                       cupos_disponibles=1)]

    async def _listar(self):
        if self.falla:
            raise self.falla
        for curso in self.cursos:
            yield curso

    def ListarCursos(self, request, timeout=None, metadata=None):
        self.metadata = dict(metadata or [])
        return self._listar()

    async def ConsultarCurso(self, request, timeout=None, metadata=None):
        if self.falla:
            raise self.falla
        for curso in self.cursos:
            if curso.id == request.curso_id:
                return curso
        raise FallaRpc(grpc.StatusCode.NOT_FOUND)


ESTUDIANTE = {"id": "est-001", "nombre": "Ana Pérez", "email": "ana@example.com"}
INSCRIPCION = {"inscripcion_id": "i-1", "estudiante_id": "est-001", "curso_id": "ARQ-101", "estado": "activa",
               "creada_en": "2026-11-22T16:00:00+00:00"}
COBRO = {"cobro_id": "i-1", "estudiante_id": "est-001", "curso_id": "ARQ-101", "monto": 150000,
         "estado": "cobrado", "creado_en": "2026-11-22T16:00:00+00:00"}


class BffTest(unittest.TestCase):
    def setUp(self):
        main.TIMEOUT = 0.3
        self.fallas: dict[str, object] = {}  # host -> status code, excepción o "lento"
        self.recibidas: list[httpx.Request] = []
        self.cliente = TestClient(main.app)
        self.cliente.__enter__()
        self.addCleanup(self.cliente.__exit__, None, None, None)

        async def manejar(request: httpx.Request):
            self.recibidas.append(request)
            falla = self.fallas.get(request.url.host)
            if falla == "lento":
                await asyncio.sleep(2)
            elif isinstance(falla, int):
                return httpx.Response(falla, json={"status": falla})
            elif falla is not None:
                raise falla
            path = request.url.path
            if path == "/estudiantes/est-001":
                return httpx.Response(200, json=ESTUDIANTE)
            if path.startswith("/estudiantes/"):
                return httpx.Response(404, json={"status": 404})
            if path == "/inscripciones":
                return httpx.Response(200, json={"items": [dict(INSCRIPCION)]})
            if path == "/cobros":
                return httpx.Response(200, json={"items": [COBRO]})
            return httpx.Response(500)

        main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(manejar))
        self.cupos = CuposFalso()
        main.app.state.cupos = self.cupos

    def panel(self, estudiante_id="est-001", **cabeceras):
        return self.cliente.get(f"/web/alumnos/{estudiante_id}/panel", headers=cabeceras)

    def test_panel_completo_junta_los_tres_servicios(self):
        r = self.panel(**{"X-Correlation-Id": "cid-1", "X-Client-Id": "portal-web"})
        self.assertEqual(r.status_code, 200)
        cuerpo = r.json()
        self.assertEqual(cuerpo["estudiante"], ESTUDIANTE)
        self.assertEqual(cuerpo["inscripciones"]["items"][0]["curso_nombre"], "Arquitectura de Sistemas")
        self.assertEqual(len(cuerpo["cursos"]["items"]), 2)
        self.assertEqual(cuerpo["pagos"], {"disponible": True, "items": [COBRO]})
        self.assertFalse(cuerpo["degradado"])
        # Propaga correlation id y cliente a HTTP y a gRPC.
        for enviada in self.recibidas:
            self.assertEqual(enviada.headers["X-Correlation-Id"], "cid-1")
            self.assertEqual(enviada.headers["X-Client-Id"], "portal-web")
        self.assertEqual(self.cupos.metadata["x-correlation-id"], "cid-1")
        consultas = {str(req.url) for req in self.recibidas}
        self.assertIn("http://pagos:8000/cobros?estudiante_id=est-001", consultas)
        self.assertIn("http://inscripcion:8000/inscripciones?estudiante_id=est-001", consultas)

    def test_pagos_caido_degrada_solo_su_seccion(self):
        antes = REGISTRY.get_sample_value("bff_degradaciones_total",
                                          {"dependencia": "pagos", "motivo": "sin_conexion"}) or 0
        self.fallas["pagos"] = httpx.ConnectError("rechazada")
        r = self.panel()
        self.assertEqual(r.status_code, 200)
        cuerpo = r.json()
        self.assertEqual(cuerpo["pagos"]["disponible"], False)
        self.assertEqual(cuerpo["pagos"]["motivo"], "sin_conexion")
        self.assertTrue(cuerpo["inscripciones"]["disponible"])
        self.assertTrue(cuerpo["cursos"]["disponible"])
        self.assertTrue(cuerpo["degradado"])
        despues = REGISTRY.get_sample_value("bff_degradaciones_total",
                                            {"dependencia": "pagos", "motivo": "sin_conexion"})
        self.assertEqual(despues - antes, 1)

    def test_pagos_lento_no_arrastra_al_panel(self):
        self.fallas["pagos"] = "lento"
        r = self.panel()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pagos"]["motivo"], "timeout")
        self.assertLess(r.elapsed.total_seconds(), 1.5)

    def test_pagos_con_error_503(self):
        self.fallas["pagos"] = 503
        self.assertEqual(self.panel().json()["pagos"]["motivo"], "error_503")

    def test_cupos_caido_degrada_cursos_sin_nombre_de_curso(self):
        self.cupos.falla = FallaRpc(grpc.StatusCode.UNAVAILABLE)
        cuerpo = self.panel().json()
        self.assertEqual(cuerpo["cursos"]["motivo"], "sin_conexion")
        self.assertNotIn("curso_nombre", cuerpo["inscripciones"]["items"][0])
        self.assertTrue(cuerpo["degradado"])

    def test_estudiante_inexistente_404(self):
        r = self.panel("nadie")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers["content-type"], "application/problem+json")

    def test_inscripcion_caida_no_hay_panel(self):
        self.fallas["inscripcion"] = httpx.ConnectError("rechazada")
        self.assertEqual(self.panel().status_code, 503)
        self.fallas["inscripcion"] = "lento"
        self.assertEqual(self.panel().status_code, 504)

    def test_cursos(self):
        r = self.cliente.get("/web/cursos")
        self.assertEqual([c["id"] for c in r.json()["items"]], ["ARQ-101", "API-201"])
        self.assertEqual(self.cliente.get("/web/cursos/API-201").json()["cupos_disponibles"], 1)
        self.assertEqual(self.cliente.get("/web/cursos/NO-EXISTE").status_code, 404)
        self.cupos.falla = FallaRpc(grpc.StatusCode.DEADLINE_EXCEEDED)
        self.assertEqual(self.cliente.get("/web/cursos").status_code, 504)

    def test_metricas_expuestas(self):
        self.panel()
        texto = self.cliente.get("/metrics").text
        self.assertIn('http_request_duration_seconds_count{route="/web/alumnos/{estudiante_id}/panel",'
                      'service="bff-web"}', texto)


if __name__ == "__main__":
    unittest.main()
