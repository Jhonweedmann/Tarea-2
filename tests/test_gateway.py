"""Pruebas del gateway: auth, rate limit por cliente, correlation id y reenvío (servicios simulados)."""
import os
import unittest

os.environ["METRICAS_PUERTO"] = "0"  # no abrir el puerto de métricas durante las pruebas

import httpx  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

from gateway.app import main  # noqa: E402
from gateway.app.rate_limit import LimitadorVentanaFija  # noqa: E402

ESCRITURA = {"Authorization": "Bearer desarrollo-seguro"}
LECTURA = {"Authorization": "Bearer solo-lectura"}
PORTAL = {"Authorization": "Bearer portal-web"}


class PipelineFalso:
    def __init__(self, redis_falso):
        self.redis, self.ops = redis_falso, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def incr(self, clave):
        self.ops.append(("incr", clave))

    def expire(self, clave, segundos):
        self.ops.append(("expire", clave))

    async def execute(self):
        if self.redis.caido:
            raise redis.ConnectionError("Redis detenido")
        resultados = []
        for op, clave in self.ops:
            if op == "incr":
                self.redis.datos[clave] = self.redis.datos.get(clave, 0) + 1
                resultados.append(self.redis.datos[clave])
            else:
                resultados.append(True)
        return resultados


class RedisFalso:
    def __init__(self):
        self.datos, self.caido = {}, False

    def pipeline(self, transaction=True):
        return PipelineFalso(self)


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.recibidas: list[httpx.Request] = []
        self.respuesta = lambda req: httpx.Response(200, json={"ok": True, "url": str(req.url)})
        self.cliente = TestClient(main.app)
        self.cliente.__enter__()
        self.addCleanup(self.cliente.__exit__, None, None, None)

        def manejar(request: httpx.Request):
            self.recibidas.append(request)
            return self.respuesta(request)

        main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(manejar))
        self.redis = RedisFalso()
        main.app.state.limitador = LimitadorVentanaFija(self.redis, 10, reloj=lambda: 1000.0)

    # --- autenticación -------------------------------------------------------------------------
    def test_health_es_publico_y_trae_correlation_id(self):
        r = self.cliente.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["X-Correlation-Id"])

    def test_sin_token_responde_401_problem(self):
        r = self.cliente.get("/api/v1/sagas/abc")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["content-type"], "application/problem+json")
        self.assertEqual(r.headers["WWW-Authenticate"], "Bearer")
        self.assertTrue(r.headers["X-Correlation-Id"])
        self.assertEqual(self.recibidas, [])

    def test_token_desconocido_responde_401_invalid_token(self):
        r = self.cliente.get("/api/v1/sagas/abc", headers={"Authorization": "Bearer otro"})
        self.assertEqual(r.status_code, 401)
        self.assertIn('error="invalid_token"', r.headers["WWW-Authenticate"])

    def test_token_de_lectura_no_puede_escribir(self):
        r = self.cliente.post("/api/v1/matriculas", headers=LECTURA, json={})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.recibidas, [])
        self.assertEqual(self.cliente.get("/api/v1/sagas/abc", headers=LECTURA).status_code, 200)

    # --- reenvío -------------------------------------------------------------------------------
    def test_reenvia_al_servicio_con_cabeceras_del_gateway(self):
        r = self.cliente.get("/api/v1/sagas/abc?detalle=1", headers={
            **ESCRITURA, "X-Client-Id": "suplantado", "X-Correlation-Id": "demo-123"})
        self.assertEqual(r.status_code, 200)
        enviada = self.recibidas[0]
        self.assertEqual(str(enviada.url), "http://orquestador:8000/sagas/abc?detalle=1")
        self.assertEqual(enviada.headers["X-Client-Id"], "secretaria")
        self.assertEqual(enviada.headers["X-Correlation-Id"], "demo-123")
        self.assertNotIn("authorization", enviada.headers)
        self.assertEqual(r.headers["X-Correlation-Id"], "demo-123")

    def test_correlation_id_invalido_se_reemplaza(self):
        r = self.cliente.get("/api/v1/sagas/abc", headers={**ESCRITURA, "X-Correlation-Id": "con espacios"})
        cid = r.headers["X-Correlation-Id"]
        self.assertNotEqual(cid, "con espacios")
        self.assertEqual(self.recibidas[0].headers["X-Correlation-Id"], cid)

    def test_post_conserva_cuerpo_idempotency_key_y_respuesta(self):
        self.respuesta = lambda req: httpx.Response(201, json={"saga_id": "s1"}, headers={"Location": "/sagas/s1"})
        r = self.cliente.post("/api/v1/matriculas", headers={**ESCRITURA, "Idempotency-Key": "k-1"},
                              json={"estudiante_id": "est-001", "curso_id": "ARQ-101"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json(), {"saga_id": "s1"})
        self.assertEqual(r.headers["Location"], "/sagas/s1")
        enviada = self.recibidas[0]
        self.assertEqual(str(enviada.url), "http://orquestador:8000/matriculas")
        self.assertEqual(enviada.headers["Idempotency-Key"], "k-1")
        self.assertEqual(enviada.content, b'{"estudiante_id":"est-001","curso_id":"ARQ-101"}')

    def test_tabla_de_rutas(self):
        casos = {
            "/api/v1/cursos": "http://bff-web:8000/web/cursos",
            "/api/v1/cursos/ARQ-101": "http://bff-web:8000/web/cursos/ARQ-101",
            "/api/v1/web/alumnos/est-001/panel": "http://bff-web:8000/web/alumnos/est-001/panel",
            "/api/v1/estudiantes/est-001": "http://inscripcion:8000/estudiantes/est-001",
        }
        for publica, interna in casos.items():
            with self.subTest(publica):
                self.cliente.get(publica, headers=ESCRITURA)
                self.assertEqual(str(self.recibidas[-1].url), interna)

    def test_ruta_desconocida_404_y_sin_token_401(self):
        self.assertEqual(self.cliente.get("/api/v1/admin/falla", headers=ESCRITURA).status_code, 404)
        self.assertEqual(self.cliente.get("/api/v1/admin/falla").status_code, 401)
        self.assertEqual(self.recibidas, [])

    def test_no_permite_salir_del_prefijo_con_punto_punto(self):
        r = self.cliente.post("/api/v1/estudiantes/%2e%2e/admin/falla", headers=ESCRITURA, json={"caido": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.recibidas, [])

    def test_timeout_del_servicio_responde_504(self):
        def lento(req):
            raise httpx.ReadTimeout("lento", request=req)
        self.respuesta = lento
        r = self.cliente.get("/api/v1/sagas/abc", headers=ESCRITURA)
        self.assertEqual(r.status_code, 504)
        self.assertEqual(r.headers["content-type"], "application/problem+json")

    def test_servicio_caido_responde_502(self):
        def caido(req):
            raise httpx.ConnectError("rechazada", request=req)
        self.respuesta = caido
        self.assertEqual(self.cliente.get("/api/v1/sagas/abc", headers=ESCRITURA).status_code, 502)

    # --- rate limit ----------------------------------------------------------------------------
    def test_rate_limit_es_por_cliente(self):
        antes = REGISTRY.get_sample_value("gateway_rate_limited_total", {"cliente": "secretaria"}) or 0
        for i in range(20):
            r = self.cliente.get("/api/v1/sagas/abc", headers=ESCRITURA)
            self.assertEqual(r.status_code, 200, i)
        self.assertEqual(r.headers["X-RateLimit-Remaining"], "0")
        r = self.cliente.get("/api/v1/sagas/abc", headers=ESCRITURA)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers["Retry-After"], "10")
        self.assertEqual(r.headers["content-type"], "application/problem+json")
        self.assertEqual(len(self.recibidas), 20)
        # Otro cliente no se ve afectado por el límite de "secretaria".
        self.assertEqual(self.cliente.get("/api/v1/sagas/abc", headers=PORTAL).status_code, 200)
        despues = REGISTRY.get_sample_value("gateway_rate_limited_total", {"cliente": "secretaria"})
        self.assertEqual(despues - antes, 1)

    def test_sin_redis_deja_pasar(self):
        self.redis.caido = True
        r = self.cliente.get("/api/v1/sagas/abc", headers=ESCRITURA)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("X-RateLimit-Limit", r.headers)

    # --- métricas ------------------------------------------------------------------------------
    def test_metricas_por_prefijo_publico(self):
        etiquetas = {"service": "gateway", "route": "/api/v1/sagas", "status": "200"}
        antes = REGISTRY.get_sample_value("http_requests_total", etiquetas) or 0
        self.cliente.get("/api/v1/sagas/uno", headers=ESCRITURA)
        self.cliente.get("/api/v1/sagas/dos", headers=ESCRITURA)
        self.assertEqual(REGISTRY.get_sample_value("http_requests_total", etiquetas) - antes, 2)


if __name__ == "__main__":
    unittest.main()
