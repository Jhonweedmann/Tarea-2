"""Cliente de Pagos (paso 2 de la Saga y su compensación).

VERSIÓN MÍNIMA escrita por B para no bloquearse: solo timeout. La Persona C la reemplaza por la
versión con reintentos con backoff, circuit breaker, bulkhead y fallback (ver resiliencia.py),
manteniendo esta interfaz para que la Saga no cambie:

    PagosClient.cobrar(cobro_id, estudiante_id, curso_id, monto) -> dict
    PagosClient.reembolsar(cobro_id) -> dict
    PagosRechazado       Pagos respondió 4xx: el cobro seguro no se aplicó.
    PagosNoDisponible    timeout, caída o circuito abierto. La Saga responde 503 + Retry-After.
      .puede_haberse_aplicado  False si la solicitud nunca salió (no conectó, circuito abierto,
                               bulkhead lleno): entonces no hace falta reembolsar.
      .retry_after             segundos sugeridos al cliente antes de reintentar.
"""
import os

import httpx

from comun.observabilidad import cabeceras_salientes


class PagosRechazado(Exception):
    def __init__(self, motivo: str, status: int):
        super().__init__(motivo)
        self.status = status


class PagosNoDisponible(Exception):
    def __init__(self, motivo: str, *, puede_haberse_aplicado: bool = True, retry_after: int = 5):
        super().__init__(motivo)
        self.puede_haberse_aplicado = puede_haberse_aplicado
        self.retry_after = retry_after


class PagosClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=(base_url or os.getenv("PAGOS_URL", "http://localhost:8002")).rstrip("/"),
            timeout=timeout or float(os.getenv("PAGOS_TIMEOUT_SECONDS", "2")),
            transport=transport,
        )

    def _post(self, ruta: str, cuerpo: dict | None = None) -> dict:
        try:
            respuesta = self.http.post(ruta, json=cuerpo, headers=cabeceras_salientes())
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise PagosNoDisponible(f"No se pudo conectar con Pagos: {type(error).__name__}",
                                    puede_haberse_aplicado=False) from error
        except httpx.HTTPError as error:
            raise PagosNoDisponible(f"Pagos no respondió: {type(error).__name__}") from error
        if respuesta.status_code >= 500:
            raise PagosNoDisponible(f"Pagos respondió {respuesta.status_code}",
                                    puede_haberse_aplicado=respuesta.status_code != 503)
        if respuesta.status_code >= 400:
            raise PagosRechazado(f"Pagos rechazó la operación ({respuesta.status_code})", respuesta.status_code)
        return respuesta.json()

    def cobrar(self, cobro_id: str, estudiante_id: str, curso_id: str, monto: int) -> dict:
        return self._post("/cobros", {"cobro_id": cobro_id, "estudiante_id": estudiante_id,
                                      "curso_id": curso_id, "monto": monto})

    def reembolsar(self, cobro_id: str) -> dict:
        return self._post(f"/cobros/{cobro_id}/reembolso")

    def close(self) -> None:
        self.http.close()
