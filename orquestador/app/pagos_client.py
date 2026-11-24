"""Cliente de Pagos (paso 2 de la Saga y su compensación), con resiliencia (ADR-008).

Interfaz que usa la Saga (no cambia respecto de la versión mínima de B):

    PagosClient.cobrar(cobro_id, estudiante_id, curso_id, monto) -> dict
    PagosClient.reembolsar(cobro_id) -> dict
    PagosRechazado       Pagos respondió 4xx: el cobro seguro no se aplicó.
    PagosNoDisponible    timeout, caída, circuito abierto o bulkhead lleno. La Saga responde 503 + Retry-After.
      .puede_haberse_aplicado  False si la solicitud nunca salió (circuito abierto, bulkhead lleno, no
                               conectó, 503): entonces la Saga solo libera el cupo y no reembolsa.
      .retry_after             segundos sugeridos al cliente antes de reintentar.

Cada llamada atraviesa las capas en este orden (de afuera hacia adentro):

    reintento con backoff  ->  circuit breaker  ->  bulkhead  ->  HTTP con timeout

- Reintento: solo para fallas transitorias (timeout, error de red, 500/502/504) y solo en `cobrar`.
  Es seguro porque el cobro es idempotente por cobro_id. `reembolsar` no se reintenta aquí: si falla,
  la Saga queda en COMPENSACION_PENDIENTE y el worker la reintenta con backoff, sin tener al cliente esperando.
- Circuit breaker: con el circuito abierto no se llama a Pagos. Ese es el fallback (degradación elegante):
  PagosNoDisponible(puede_haberse_aplicado=False) en milisegundos; la Saga libera el cupo y responde
  503 + Retry-After con los segundos que faltan para que el circuito pruebe de nuevo.
- Bulkhead: máximo de llamadas en vuelo; el exceso se rechaza al instante con el mismo fallback.
- Timeout: connect corto y read acotado, en toda llamada.

RESILIENCIA=off desactiva reintento, circuit breaker y bulkhead (queda solo el timeout). Existe para
la corrida "sin patrón" del escenario 3: comparar el mismo sistema con y sin protección.
"""
import math
import os
import random
import time
from typing import Callable

import httpx
from prometheus_client import Counter, Histogram

from comun.observabilidad import cabeceras_salientes, obtener_logger

from .resiliencia import Bulkhead, BulkheadLleno, CircuitBreaker, CircuitoAbierto

DEP = "pagos"

LLAMADAS = Counter("pagos_llamadas_total", "Llamadas del orquestador a Pagos, por operación y resultado",
                   ["operacion", "resultado"])
DURACION = Histogram("pagos_llamada_duracion_seconds", "Duración de las llamadas HTTP que sí salieron hacia Pagos",
                     ["operacion"], buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 2.5, 5, 10, 30))
REINTENTOS = Counter("pagos_reintentos_total", "Reintentos con backoff hacia Pagos", ["operacion"])
log = obtener_logger("orquestador")


class PagosRechazado(Exception):
    def __init__(self, motivo: str, status: int):
        super().__init__(motivo)
        self.status = status


class PagosNoDisponible(Exception):
    def __init__(self, motivo: str, *, puede_haberse_aplicado: bool = True, retry_after: int = 5,
                 reintentable: bool = False):
        super().__init__(motivo)
        self.puede_haberse_aplicado = puede_haberse_aplicado
        self.retry_after = retry_after
        # Solo lo usa el reintento de este módulo: ¿tiene sentido volver a intentar enseguida?
        self.reintentable = reintentable


def _entorno(nombre: str, defecto, tipo=float):
    return tipo(os.getenv(nombre, defecto))


class PagosClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None, *, resiliencia: bool | None = None,
                 breaker: CircuitBreaker | None = None, bulkhead: Bulkhead | None = None,
                 reintentos_cobro: int | None = None, backoff_base: float | None = None,
                 dormir: Callable[[float], None] = time.sleep):
        self.resiliencia = (os.getenv("RESILIENCIA", "on").lower() != "off") if resiliencia is None else resiliencia
        self.http = httpx.Client(
            base_url=(base_url or os.getenv("PAGOS_URL", "http://localhost:8002")).rstrip("/"),
            # connect corto: si Pagos no atiende, se sabe rápido y además la solicitud no salió.
            timeout=httpx.Timeout(timeout or _entorno("PAGOS_TIMEOUT_SECONDS", 2),
                                  connect=_entorno("PAGOS_CONNECT_TIMEOUT_SECONDS", 1)),
            transport=transport,
        )
        self.breaker = breaker or CircuitBreaker(
            DEP,
            umbral_fallos=_entorno("PAGOS_CB_UMBRAL", 0.5),
            min_llamadas=_entorno("PAGOS_CB_MIN_LLAMADAS", 5, int),
            ventana_s=_entorno("PAGOS_CB_VENTANA_SECONDS", 10),
            espera_s=_entorno("PAGOS_CB_ESPERA_SECONDS", 5),
            exitos_para_cerrar=_entorno("PAGOS_CB_EXITOS_CIERRE", 2, int),
        )
        self.bulkhead = bulkhead or Bulkhead(DEP, _entorno("PAGOS_BULKHEAD_MAX", 5, int))
        self.reintentos_cobro = _entorno("PAGOS_REINTENTOS", 1, int) if reintentos_cobro is None else reintentos_cobro
        self.backoff_base = _entorno("PAGOS_BACKOFF_BASE_SECONDS", 0.2) if backoff_base is None else backoff_base
        self._dormir = dormir
        log.info("cliente de Pagos listo", extra={
            "resiliencia": self.resiliencia, "timeout_s": self.http.timeout.read,
            "bulkhead_max": self.bulkhead.maximo, "reintentos_cobro": self.reintentos_cobro})

    # ------------------------------------------------------------ API que usa la Saga

    def cobrar(self, cobro_id: str, estudiante_id: str, curso_id: str, monto: int) -> dict:
        return self._llamar("cobrar", "/cobros", {"cobro_id": cobro_id, "estudiante_id": estudiante_id,
                                                  "curso_id": curso_id, "monto": monto},
                            reintentos=self.reintentos_cobro)

    def reembolsar(self, cobro_id: str) -> dict:
        # Sin reintento inmediato: la compensación fallida la retoma el worker (backoff exponencial).
        return self._llamar("reembolsar", f"/cobros/{cobro_id}/reembolso", None, reintentos=0)

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------ Capas

    def _llamar(self, operacion: str, ruta: str, cuerpo: dict | None, reintentos: int) -> dict:
        if not self.resiliencia:
            return self._enviar(operacion, ruta, cuerpo)  # "sin patrón": solo timeout
        intento = 0
        incierto = False
        while True:
            try:
                return self._protegida(operacion, ruta, cuerpo)
            except PagosNoDisponible as error:
                # La incertidumbre se arrastra entre intentos: si un intento anterior pudo aplicarse (p. ej.
                # un timeout) y el siguiente lo corta el circuito, el cobro SIGUE siendo incierto y la
                # Saga debe reembolsar. Sin esto quedaría un cobro huérfano.
                incierto = incierto or error.puede_haberse_aplicado
                error.puede_haberse_aplicado = incierto
                if not error.reintentable or intento >= reintentos:
                    raise
                espera = self.backoff_base * 2 ** intento * (0.5 + random.random() / 2)
                intento += 1
                REINTENTOS.labels(operacion).inc()
                log.info("reintento hacia Pagos", extra={"operacion": operacion, "intento": intento,
                                                         "espera_s": round(espera, 3), "motivo": str(error)})
                self._dormir(espera)

    def _protegida(self, operacion: str, ruta: str, cuerpo: dict | None) -> dict:
        """Circuit breaker + bulkhead alrededor de una sola llamada HTTP."""
        try:
            self.breaker.antes_de_llamar()
        except CircuitoAbierto as abierto:
            # Fallback: no se llama a Pagos. La solicitud nunca salió, así que no hay nada que reembolsar.
            LLAMADAS.labels(operacion, "circuito_abierto").inc()
            raise PagosNoDisponible(
                f"Pagos no disponible: circuito abierto (próximo intento en {abierto.reintentar_en:.0f} s)",
                puede_haberse_aplicado=False, retry_after=max(1, math.ceil(abierto.reintentar_en))) from abierto
        try:
            self.bulkhead.adquirir()
        except BulkheadLleno as lleno:
            self.breaker.cancelar()  # no fue un fallo de Pagos: se devuelve el permiso del circuito
            LLAMADAS.labels(operacion, "bulkhead_lleno").inc()
            raise PagosNoDisponible("Pagos saturado: demasiadas llamadas en curso",
                                    puede_haberse_aplicado=False, retry_after=1) from lleno
        try:
            resultado = self._enviar(operacion, ruta, cuerpo)
        except PagosRechazado:
            self.breaker.registrar_exito()  # Pagos respondió (un 4xx): está vivo
            raise
        except PagosNoDisponible:
            self.breaker.registrar_fallo()
            raise
        except BaseException:
            self.breaker.cancelar()
            raise
        else:
            self.breaker.registrar_exito()
            return resultado
        finally:
            self.bulkhead.liberar()

    def _enviar(self, operacion: str, ruta: str, cuerpo: dict | None) -> dict:
        """Una llamada HTTP con timeout, clasificada según si la operación pudo aplicarse."""
        inicio = time.perf_counter()
        resultado = "ok"
        try:
            try:
                respuesta = self.http.post(ruta, json=cuerpo, headers=cabeceras_salientes())
            except (httpx.ConnectTimeout, httpx.ConnectError) as error:
                resultado = "sin_conexion"
                raise PagosNoDisponible(f"No se pudo conectar con Pagos: {type(error).__name__}",
                                        puede_haberse_aplicado=False, reintentable=True) from error
            except httpx.TimeoutException as error:
                resultado = "timeout"
                raise PagosNoDisponible(f"Pagos no respondió: {type(error).__name__}",
                                        reintentable=True) from error
            except httpx.HTTPError as error:
                resultado = "error_red"
                raise PagosNoDisponible(f"Pagos no respondió: {type(error).__name__}",
                                        reintentable=True) from error
            if respuesta.status_code >= 500:
                resultado = f"http_{respuesta.status_code}"
                # 503 = Pagos dice que no procesó nada (y que no vale la pena insistir ya mismo).
                raise PagosNoDisponible(f"Pagos respondió {respuesta.status_code}",
                                        puede_haberse_aplicado=respuesta.status_code != 503,
                                        reintentable=respuesta.status_code != 503)
            if respuesta.status_code >= 400:
                resultado = "rechazo"
                raise PagosRechazado(f"Pagos rechazó la operación ({respuesta.status_code})", respuesta.status_code)
            return respuesta.json()
        finally:
            DURACION.labels(operacion).observe(time.perf_counter() - inicio)
            LLAMADAS.labels(operacion, resultado).inc()
