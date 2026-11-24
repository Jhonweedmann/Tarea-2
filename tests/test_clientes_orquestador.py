"""Clientes HTTP del orquestador: cómo clasifican cada falla (¿pudo aplicarse la operación o no?)."""
import unittest

import httpx

from orquestador.app.inscripcion_client import InscripcionClient, InscripcionError
from orquestador.app.pagos_client import PagosClient, PagosNoDisponible, PagosRechazado


def transporte(respuesta=None, error=None, registro=None):
    def manejar(request: httpx.Request):
        if registro is not None:
            registro.append(request)
        if error is not None:
            raise error
        return respuesta
    return httpx.MockTransport(manejar)


class PagosClientTests(unittest.TestCase):
    def cliente(self, **kwargs):
        return PagosClient(base_url="http://pagos", transport=transporte(**kwargs))

    def test_cobro_exitoso_envia_el_saga_id_como_cobro_id(self):
        registro = []
        cobro = {"cobro_id": "saga-1", "estado": "cobrado"}
        cliente = self.cliente(respuesta=httpx.Response(201, json=cobro), registro=registro)
        self.assertEqual(cliente.cobrar("saga-1", "est-001", "ARQ-101", 150000), cobro)
        self.assertEqual(registro[0].url.path, "/cobros")
        self.assertIn(b'"cobro_id":"saga-1"', registro[0].content)

    def test_4xx_es_rechazo_definitivo(self):
        with self.assertRaises(PagosRechazado) as caso:
            self.cliente(respuesta=httpx.Response(409, json={})).cobrar("s", "e", "c", 1)
        self.assertEqual(caso.exception.status, 409)

    def test_clasifica_si_el_cobro_pudo_aplicarse(self):
        casos = [
            ({"error": httpx.ConnectError("rechazada")}, False),      # nunca salió
            ({"error": httpx.ConnectTimeout("lento")}, False),        # nunca salió
            ({"respuesta": httpx.Response(503, json={})}, False),     # el servicio dijo que no lo procesó
            ({"error": httpx.ReadTimeout("lento")}, True),            # salió y no hubo respuesta
            ({"respuesta": httpx.Response(500, json={})}, True),      # pudo fallar después de cobrar
        ]
        for kwargs, esperado in casos:
            with self.subTest(kwargs=kwargs), self.assertRaises(PagosNoDisponible) as caso:
                self.cliente(**kwargs).reembolsar("saga-1")
            self.assertEqual(caso.exception.puede_haberse_aplicado, esperado)


class InscripcionClientTests(unittest.TestCase):
    def cliente(self, **kwargs):
        return InscripcionClient(base_url="http://inscripcion", transport=transporte(**kwargs))

    def test_estudiante_existe(self):
        self.assertTrue(self.cliente(respuesta=httpx.Response(200, json={"id": "est-001"})).estudiante_existe("x"))
        self.assertFalse(self.cliente(respuesta=httpx.Response(404, json={})).estudiante_existe("x"))
        with self.assertRaises(InscripcionError):
            self.cliente(respuesta=httpx.Response(503, json={})).estudiante_existe("x")

    def test_inscribir_rechazado_lleva_el_codigo(self):
        with self.assertRaises(InscripcionError) as caso:
            self.cliente(respuesta=httpx.Response(409, json={"title": "Inscripción duplicada"})).inscribir("s", "e", "c")
        self.assertEqual((caso.exception.status, caso.exception.incierta), (409, False))
        self.assertIn("Inscripción duplicada", str(caso.exception))

    def test_clasifica_si_la_inscripcion_pudo_aplicarse(self):
        casos = [
            ({"error": httpx.ConnectError("rechazada")}, False),
            ({"respuesta": httpx.Response(503, json={})}, False),
            ({"error": httpx.ReadTimeout("lento")}, True),
            ({"respuesta": httpx.Response(500, json={})}, True),
        ]
        for kwargs, esperado in casos:
            with self.subTest(kwargs=kwargs), self.assertRaises(InscripcionError) as caso:
                self.cliente(**kwargs).inscribir("saga-1", "est-001", "ARQ-101")
            self.assertEqual(caso.exception.incierta, esperado)


if __name__ == "__main__":
    unittest.main()
