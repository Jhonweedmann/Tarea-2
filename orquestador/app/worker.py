"""Worker de compensaciones: termina las sagas que no pudieron deshacerse a la primera.

Corre en un hilo del orquestador y, cada `intervalo` segundos:
1. Reintenta las sagas en COMPENSACION_PENDIENTE cuyo próximo intento ya llegó. Si vuelve a fallar,
   espera el doble que la vez anterior (backoff exponencial: 1, 2, 4, 8... s, con tope), para no
   martillar a un servicio que está caído. Reintentar es seguro porque las compensaciones son
   idempotentes.
2. Retoma las sagas "estancadas": quedaron a medio camino (INICIADA, CUPO_RESERVADO, PAGO_COBRADO o
   COMPENSANDO) sin cambios desde hace rato, lo que solo pasa si el orquestador se cayó mientras
   corrían. El paso que estaba en curso se marca incierto y se deshace todo lo aplicado.

Supone una sola instancia del orquestador (con varias habría que repartir las sagas entre ellas).
"""
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from comun.observabilidad import obtener_logger

from .database import RepositorioSagas
from .saga import Saga

# Paso que estaba en curso según el último estado guardado.
PASO_EN_CURSO = {"INICIADA": "reservar_cupo", "CUPO_RESERVADO": "cobrar", "PAGO_COBRADO": "inscribir"}

log = obtener_logger("orquestador")


class WorkerCompensaciones:
    def __init__(self, repo: RepositorioSagas, crear_saga: Callable[[], Saga], intervalo: float = 2.0,
                 espera_base: float = 1.0, espera_maxima: float = 60.0, estancada_tras: float = 60.0):
        self.repo = repo
        self.crear_saga = crear_saga
        self.intervalo = intervalo
        self.espera_base = espera_base
        self.espera_maxima = espera_maxima
        self.estancada_tras = estancada_tras
        self._detener = threading.Event()
        self._hilo: threading.Thread | None = None

    def procesar(self, ahora: datetime | None = None) -> int:
        """Una pasada del worker. Devuelve cuántas sagas intentó terminar."""
        ahora = ahora or datetime.now(timezone.utc)
        procesadas = 0
        for saga in self.repo.estancadas((ahora - timedelta(seconds=self.estancada_tras)).isoformat()):
            self._retomar_estancada(saga)
            procesadas += 1
        for saga_id in self.repo.pendientes(ahora.isoformat()):
            self._reintentar(saga_id, ahora)
            procesadas += 1
        return procesadas

    def _retomar_estancada(self, saga: dict) -> None:
        saga_id = saga["saga_id"]
        if paso := PASO_EN_CURSO.get(saga["estado"]):
            self.repo.registrar_paso(saga_id, paso, "incierto", "el orquestador se interrumpió durante este paso")
        self.repo.cambiar_estado(saga_id, "COMPENSANDO",
                                 estado_tras_compensar=saga["estado_tras_compensar"] or "COMPENSADA",
                                 error=saga["error"] or "Saga interrumpida: se deshace lo aplicado")
        log.warning("saga estancada, se compensa", extra={"saga_id": saga_id, "estado": saga["estado"]})
        # Si la compensación falla queda en COMPENSACION_PENDIENTE y sigue por el camino normal.
        self.crear_saga().compensar(saga_id)

    def _reintentar(self, saga_id: str, ahora: datetime) -> None:
        intentos = self.repo.obtener(saga_id)["intentos_compensacion"] + 1
        log.info("reintentando compensación", extra={"saga_id": saga_id, "intento": intentos})
        if self.crear_saga().compensar(saga_id):
            return
        espera = min(self.espera_base * 2 ** (intentos - 1), self.espera_maxima)
        self.repo.cambiar_estado(saga_id, "COMPENSACION_PENDIENTE", intentos_compensacion=intentos,
                                 proximo_intento=(ahora + timedelta(seconds=espera)).isoformat())
        log.info("compensación sigue pendiente", extra={"saga_id": saga_id, "intento": intentos,
                                                         "proximo_intento_s": espera})

    # ------------------------------------------------------------ Hilo

    def iniciar(self) -> None:
        self._hilo = threading.Thread(target=self._bucle, name="worker-compensaciones", daemon=True)
        self._hilo.start()

    def _bucle(self) -> None:
        while not self._detener.wait(self.intervalo):
            try:
                self.procesar()
            except Exception:  # el worker nunca debe morir por una saga problemática
                log.exception("error en el worker de compensaciones")

    def detener(self) -> None:
        self._detener.set()
        if self._hilo is not None:
            self._hilo.join(timeout=5)
