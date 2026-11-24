"""Pruebas de resiliencia hacia Pagos: Circuit Breaker, Bulkhead, reintentos y fallback del cliente."""
import logging
import os
import threading
import unittest

import httpx
from prometheus_client import REGISTRY

os.environ.setdefault("LOG_NIVEL", "CRITICAL")
logging.getLogger("orquestador").setLevel(logging.CRITICAL)  # sin ruido de los logs del circuito
from orquestador.app.pagos_client import PagosClient, PagosNoDisponible, PagosRechazado  # noqa: E402
from orquestador.app.resiliencia import (  # noqa: E402
    ABIERTO, CERRADO, HALF_OPEN, Bulkhead, BulkheadLleno, CircuitBreaker, CircuitoAbierto)


class RelojFalso:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def avanzar(self, segundos):
        self.t += segundos


def metrica(nombre, **etiquetas):
    return REGISTRY.get_sample_value(nombre, etiquetas)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.reloj = RelojFalso()
        self.cb = CircuitBreaker("t-cb", umbral_fallos=0.5, min_llamadas=4, ventana_s=10, espera_s=5,
                                 exitos_para_cerrar=2, reloj=self.reloj)

    def llamar(self, exito):
        self.cb.antes_de_llamar()
        (self.cb.registrar_exito if exito else self.cb.registrar_fallo)()

    def abrir(self):
        for _ in range(4):
            self.llamar(False)

    def test_empieza_cerrado(self):
        self.assertEqual(self.cb.estado, CERRADO)
        self.assertEqual(metrica("circuit_state", dep="t-cb"), 0)

    def test_no_abre_con_pocas_llamadas(self):
        for _ in range(3):
            self.llamar(False)
        self.assertEqual(self.cb.estado, CERRADO)

    def test_no_abre_si_la_tasa_de_fallos_es_baja(self):
        for exito in (True, True, True, False):
            self.llamar(exito)
        self.assertEqual(self.cb.estado, CERRADO)

    def test_abre_al_alcanzar_el_umbral_y_corta_sin_llamar(self):
        self.abrir()
        self.assertEqual(self.cb.estado, ABIERTO)
        self.assertEqual(metrica("circuit_state", dep="t-cb"), 2)
        antes = metrica("circuit_rejected_total", dep="t-cb") or 0
        with self.assertRaises(CircuitoAbierto) as caso:
            self.cb.antes_de_llamar()
        self.assertAlmostEqual(caso.exception.reintentar_en, 5, places=1)
        self.assertEqual(metrica("circuit_rejected_total", dep="t-cb"), antes + 1)

    def test_los_fallos_viejos_salen_de_la_ventana(self):
        for _ in range(3):
            self.llamar(False)
        self.reloj.avanzar(11)  # los 3 fallos quedan fuera de la ventana de 10 s
        self.llamar(False)
        self.assertEqual(self.cb.estado, CERRADO)

    def test_pasa_a_half_open_tras_la_espera_y_deja_pasar_una_sola_sonda(self):
        self.abrir()
        self.reloj.avanzar(5)
        self.assertEqual(self.cb.estado, HALF_OPEN)
        self.cb.antes_de_llamar()  # la sonda
        with self.assertRaises(CircuitoAbierto):
            self.cb.antes_de_llamar()  # una segunda llamada simultánea no pasa

    def test_cierra_tras_los_exitos_necesarios_en_half_open(self):
        self.abrir()
        self.reloj.avanzar(5)
        self.llamar(True)
        self.assertEqual(self.cb.estado, HALF_OPEN)
        self.llamar(True)
        self.assertEqual(self.cb.estado, CERRADO)
        self.assertEqual(metrica("circuit_state", dep="t-cb"), 0)
        self.llamar(False)  # la ventana quedó limpia: un fallo solo no lo vuelve a abrir
        self.assertEqual(self.cb.estado, CERRADO)

    def test_un_fallo_en_half_open_reabre_y_reinicia_la_espera(self):
        self.abrir()
        self.reloj.avanzar(5)
        self.llamar(False)
        self.assertEqual(self.cb.estado, ABIERTO)
        self.assertAlmostEqual(self.cb.segundos_para_reintentar(), 5, places=1)

    def test_cancelar_devuelve_el_permiso_de_la_sonda(self):
        self.abrir()
        self.reloj.avanzar(5)
        self.cb.antes_de_llamar()
        self.cb.cancelar()
        self.cb.antes_de_llamar()  # puede pasar otra sonda

    def test_cuenta_las_transiciones(self):
        antes = metrica("circuit_transitions_total", dep="t-cb", hacia="abierto") or 0
        self.abrir()
        self.assertEqual(metrica("circuit_transitions_total", dep="t-cb", hacia="abierto"), antes + 1)


class BulkheadTests(unittest.TestCase):
    def test_rechaza_el_exceso_sin_esperar_y_libera(self):
        bh = Bulkhead("t-bh", 2)
        bh.adquirir()
        bh.adquirir()
        self.assertEqual(metrica("bulkhead_in_flight", dep="t-bh"), 2)
        antes = metrica("bulkhead_rejected_total", dep="t-bh") or 0
        with self.assertRaises(BulkheadLleno):
            bh.adquirir()
        self.assertEqual(metrica("bulkhead_rejected_total", dep="t-bh"), antes + 1)
        bh.liberar()
        bh.adquirir()  # vuelve a haber lugar
        bh.liberar()
        bh.liberar()
        self.assertEqual(metrica("bulkhead_in_flight", dep="t-bh"), 0)

    def test_con_hilos_nunca_hay_mas_del_maximo_en_vuelo(self):
        bh = Bulkhead("t-bh2", 3)
        dentro, maximo, rechazados = [0], [0], [0]
        lock, salida = threading.Lock(), threading.Event()

        def tarea():
            try:
                bh.adquirir()
            except BulkheadLleno:
                with lock:
                    rechazados[0] += 1
                return
            with lock:
                dentro[0] += 1
                maximo[0] = max(maximo[0], dentro[0])
            salida.wait(2)
            with lock:
                dentro[0] -= 1
            bh.liberar()

        hilos = [threading.Thread(target=tarea) for _ in range(10)]
        for hilo in hilos:
            hilo.start()
        while True:
            with lock:
                if dentro[0] + rechazados[0] == 10:
                    break
        salida.set()
        for hilo in hilos:
            hilo.join()
        self.assertEqual((maximo[0], rechazados[0]), (3, 7))


class ClienteResilienteTests(unittest.TestCase):
    """PagosClient con transporte simulado: qué llega (o no) a Pagos en cada situación."""

    def cliente(self, respuestas, *, reintentos=1, maximo=5, **kwargs):
        self.llamadas = []
        self.pausas = []
        guion = list(respuestas)

        def manejar(request: httpx.Request):
            self.llamadas.append(request.url.path)
            accion = guion.pop(0) if len(guion) > 1 else guion[0]
            if isinstance(accion, Exception):
                raise accion
            return accion

        self.reloj = RelojFalso()
        cb = CircuitBreaker("t-cli", min_llamadas=4, espera_s=5, exitos_para_cerrar=1, reloj=self.reloj)
        return PagosClient(base_url="http://pagos", transport=httpx.MockTransport(manejar), breaker=cb,
                           bulkhead=Bulkhead("t-cli", maximo), reintentos_cobro=reintentos, backoff_base=0.2,
                           dormir=self.pausas.append, **kwargs)

    def cobrar(self, cliente):
        return cliente.cobrar("s1", "est-001", "ARQ-101", 150000)

    def test_cobro_exitoso(self):
        cliente = self.cliente([httpx.Response(201, json={"estado": "cobrado"})])
        self.assertEqual(self.cobrar(cliente), {"estado": "cobrado"})
        self.assertEqual(self.llamadas, ["/cobros"])

    def test_cobrar_reintenta_una_falla_transitoria_con_backoff(self):
        cliente = self.cliente([httpx.ReadTimeout("lento"), httpx.Response(201, json={"estado": "cobrado"})])
        self.assertEqual(self.cobrar(cliente)["estado"], "cobrado")
        self.assertEqual(len(self.llamadas), 2)
        self.assertEqual(len(self.pausas), 1)
        self.assertTrue(0.1 <= self.pausas[0] <= 0.2)  # backoff base 0.2 s con jitter

    def test_el_backoff_crece_con_cada_reintento(self):
        cliente = self.cliente([httpx.ReadTimeout("lento")], reintentos=3)
        with self.assertRaises(PagosNoDisponible):
            self.cobrar(cliente)
        self.assertEqual(len(self.llamadas), 4)
        # Backoff exponencial con jitter: base 0.2 s, luego el doble, luego el doble otra vez.
        for pausa, (minimo, maximo) in zip(self.pausas, [(0.1, 0.2), (0.2, 0.4), (0.4, 0.8)]):
            self.assertTrue(minimo <= pausa <= maximo, (pausa, minimo, maximo))

    def test_cobrar_agota_los_reintentos_y_avisa_que_pudo_aplicarse(self):
        cliente = self.cliente([httpx.ReadTimeout("lento")])
        with self.assertRaises(PagosNoDisponible) as caso:
            self.cobrar(cliente)
        self.assertTrue(caso.exception.puede_haberse_aplicado)
        self.assertEqual(len(self.llamadas), 2)  # intento + 1 reintento

    def test_la_incertidumbre_de_un_intento_se_arrastra_al_reintento_cortado_por_el_circuito(self):
        # 1.er intento: timeout (pudo cobrarse). Ese fallo abre el circuito, así que el reintento ni sale.
        # Aun así el cobro es incierto: la Saga debe reembolsar para no dejar un cobro huérfano.
        cliente = self.cliente([httpx.ReadTimeout("lento")])
        cliente.breaker.min_llamadas = 1
        with self.assertRaises(PagosNoDisponible) as caso:
            self.cobrar(cliente)
        self.assertEqual(len(self.llamadas), 1)  # el reintento no salió
        self.assertIn("circuito abierto", str(caso.exception))
        self.assertTrue(caso.exception.puede_haberse_aplicado)

    def test_503_no_se_reintenta_y_no_pudo_aplicarse(self):
        cliente = self.cliente([httpx.Response(503, json={})])
        with self.assertRaises(PagosNoDisponible) as caso:
            self.cobrar(cliente)
        self.assertFalse(caso.exception.puede_haberse_aplicado)
        self.assertEqual((len(self.llamadas), self.pausas), (1, []))

    def test_reembolsar_no_se_reintenta_aqui_lo_hace_el_worker(self):
        cliente = self.cliente([httpx.ReadTimeout("lento")])
        with self.assertRaises(PagosNoDisponible):
            cliente.reembolsar("s1")
        self.assertEqual((len(self.llamadas), self.pausas), (1, []))

    def test_un_rechazo_4xx_no_se_reintenta_ni_cuenta_como_fallo_del_circuito(self):
        cliente = self.cliente([httpx.Response(409, json={})])
        for _ in range(6):
            with self.assertRaises(PagosRechazado):
                self.cobrar(cliente)
        self.assertEqual(cliente.breaker.estado, CERRADO)
        self.assertEqual(len(self.llamadas), 6)

    def test_el_circuito_abre_y_el_fallback_no_llama_a_pagos(self):
        cliente = self.cliente([httpx.Response(503, json={})], reintentos=0)
        for _ in range(4):
            with self.assertRaises(PagosNoDisponible):
                self.cobrar(cliente)
        self.assertEqual(cliente.breaker.estado, ABIERTO)
        llamadas_antes = len(self.llamadas)
        with self.assertRaises(PagosNoDisponible) as caso:
            self.cobrar(cliente)
        self.assertEqual(len(self.llamadas), llamadas_antes)  # fail-fast: la solicitud nunca salió
        self.assertFalse(caso.exception.puede_haberse_aplicado)
        self.assertGreaterEqual(caso.exception.retry_after, 1)
        self.assertIn("circuito abierto", str(caso.exception))

    def test_el_reembolso_tambien_se_corta_con_el_circuito_abierto(self):
        cliente = self.cliente([httpx.Response(503, json={})], reintentos=0)
        for _ in range(4):
            with self.assertRaises(PagosNoDisponible):
                self.cobrar(cliente)
        with self.assertRaises(PagosNoDisponible) as caso:
            cliente.reembolsar("s1")
        self.assertIn("circuito abierto", str(caso.exception))  # queda pendiente para el worker

    def test_se_recupera_via_half_open(self):
        cliente = self.cliente([httpx.Response(503, json={})] * 4 + [httpx.Response(201, json={"estado": "cobrado"})],
                               reintentos=0)
        for _ in range(4):
            with self.assertRaises(PagosNoDisponible):
                self.cobrar(cliente)
        self.reloj.avanzar(5)
        self.assertEqual(self.cobrar(cliente)["estado"], "cobrado")
        self.assertEqual(cliente.breaker.estado, CERRADO)

    def test_bulkhead_lleno_rechaza_rapido_sin_tocar_el_circuito(self):
        cliente = self.cliente([httpx.Response(201, json={})], maximo=1)
        cliente.bulkhead.adquirir()  # otra llamada lenta ocupa el único lugar
        with self.assertRaises(PagosNoDisponible) as caso:
            self.cobrar(cliente)
        self.assertFalse(caso.exception.puede_haberse_aplicado)
        self.assertEqual(self.llamadas, [])
        self.assertEqual(cliente.breaker.estado, CERRADO)
        cliente.bulkhead.liberar()
        self.cobrar(cliente)  # al liberarse vuelve a funcionar

    def test_el_bulkhead_se_libera_aunque_la_llamada_falle(self):
        cliente = self.cliente([httpx.ConnectError("caído")], reintentos=0, maximo=2)
        for _ in range(3):
            with self.assertRaises(PagosNoDisponible):
                self.cobrar(cliente)
        self.assertEqual(cliente.bulkhead.en_vuelo, 0)

    def test_con_resiliencia_off_no_hay_circuito_bulkhead_ni_reintentos(self):
        cliente = self.cliente([httpx.Response(500, json={})], resiliencia=False)
        for _ in range(20):
            with self.assertRaises(PagosNoDisponible):
                self.cobrar(cliente)
        self.assertEqual(len(self.llamadas), 20)  # todas llegan a Pagos, ninguna se corta ni se reintenta
        self.assertEqual((cliente.breaker.estado, self.pausas), (CERRADO, []))


if __name__ == "__main__":
    unittest.main()
