"""Cliente HTTP de Inscripción (validación previa, paso 3 de la Saga y su compensación).

La distinción importante es si la operación pudo haberse aplicado o no:
- Respuesta 4xx o 503, o no se pudo conectar: seguro que NO se aplicó.
- Timeout de lectura, conexión cortada o 5xx distinto de 503: INCIERTO (pudo aplicarse y perderse
  la respuesta). La Saga compensa también ese paso, lo que es seguro porque dar de baja es
  idempotente y deja una lápida si la inscripción nunca existió.
"""
import os

import httpx

from comun.observabilidad import cabeceras_salientes


class InscripcionError(Exception):
    def __init__(self, motivo: str, *, status: int | None = None, incierta: bool = False):
        super().__init__(motivo)
        self.status = status
        self.incierta = incierta


class InscripcionClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=(base_url or os.getenv("INSCRIPCION_URL", "http://localhost:8001")).rstrip("/"),
            timeout=timeout or float(os.getenv("INSCRIPCION_TIMEOUT_SECONDS", "2")),
            transport=transport,
        )

    def _llamar(self, metodo: str, ruta: str, **kwargs) -> httpx.Response:
        try:
            respuesta = self.http.request(metodo, ruta, headers=cabeceras_salientes(), **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            # La solicitud no alcanzó a enviarse.
            raise InscripcionError(f"Inscripción no disponible: {type(error).__name__}") from error
        except httpx.HTTPError as error:
            raise InscripcionError(f"Inscripción no respondió: {type(error).__name__}", incierta=True) from error
        if respuesta.status_code >= 500:
            incierta = respuesta.status_code != 503
            raise InscripcionError(f"Inscripción respondió {respuesta.status_code}",
                                   status=respuesta.status_code, incierta=incierta)
        return respuesta

    def estudiante_existe(self, estudiante_id: str) -> bool:
        respuesta = self._llamar("GET", f"/estudiantes/{estudiante_id}")
        if respuesta.status_code == 404:
            return False
        if respuesta.status_code >= 400:
            raise InscripcionError(f"Inscripción respondió {respuesta.status_code}", status=respuesta.status_code)
        return True

    def inscribir(self, inscripcion_id: str, estudiante_id: str, curso_id: str) -> dict:
        respuesta = self._llamar("POST", "/inscripciones", json={
            "inscripcion_id": inscripcion_id, "estudiante_id": estudiante_id, "curso_id": curso_id})
        if respuesta.status_code >= 400:
            titulo = respuesta.json().get("title", "") if respuesta.content else ""
            raise InscripcionError(f"Inscripción rechazó la inscripción ({respuesta.status_code}): {titulo}",
                                   status=respuesta.status_code)
        return respuesta.json()

    def dar_baja(self, inscripcion_id: str) -> dict:
        respuesta = self._llamar("POST", f"/inscripciones/{inscripcion_id}/baja")
        if respuesta.status_code >= 400:
            raise InscripcionError(f"Inscripción respondió {respuesta.status_code} al dar de baja",
                                   status=respuesta.status_code)
        return respuesta.json()

    def close(self) -> None:
        self.http.close()
