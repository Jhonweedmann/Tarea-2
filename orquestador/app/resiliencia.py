"""Circuit Breaker y Bulkhead (ADR-008). Implementación propia, sin librerías, para poder explicarla.

Las dos piezas protegen al orquestador de una dependencia frágil (Pagos):

- CircuitBreaker: si Pagos falla seguido, deja de llamarlo un rato (fail-fast) en vez de gastar
  un hilo y 2 s de timeout por cada petición.
- Bulkhead: limita cuántas llamadas a Pagos pueden estar en vuelo a la vez. Si Pagos se pone
  lento, solo se ocupan esos N lugares; el resto de los hilos del orquestador sigue libre.

Métricas (nombres fijos del contrato, docs/contratos/README.md):
    circuit_state{dep}              gauge: 0 cerrado, 1 half-open, 2 abierto
    bulkhead_in_flight{dep}         gauge: llamadas en vuelo
    bulkhead_rejected_total{dep}    contador: llamadas rechazadas por exceso
Métricas extra, útiles como evidencia:
    circuit_transitions_total{dep,hacia}   contador de cambios de estado
    circuit_rejected_total{dep}            llamadas cortadas con el circuito abierto (fail-fast)
"""
import threading
import time
from collections import deque
from typing import Callable

from prometheus_client import Counter, Gauge

from comun.observabilidad import obtener_logger

CERRADO, HALF_OPEN, ABIERTO = "cerrado", "half_open", "abierto"
VALOR_ESTADO = {CERRADO: 0, HALF_OPEN: 1, ABIERTO: 2}

CIRCUIT_STATE = Gauge("circuit_state", "Estado del circuito: 0 cerrado, 1 half-open, 2 abierto", ["dep"])
CIRCUIT_TRANSICIONES = Counter("circuit_transitions_total", "Cambios de estado del circuito", ["dep", "hacia"])
CIRCUIT_RECHAZOS = Counter("circuit_rejected_total", "Llamadas cortadas porque el circuito estaba abierto", ["dep"])
BULKHEAD_EN_VUELO = Gauge("bulkhead_in_flight", "Llamadas en vuelo hacia la dependencia", ["dep"])
BULKHEAD_RECHAZOS = Counter("bulkhead_rejected_total", "Llamadas rechazadas porque el bulkhead estaba lleno", ["dep"])

log = obtener_logger("orquestador")


class CircuitoAbierto(Exception):
    """El circuito está abierto: la llamada ni siquiera se intenta."""

    def __init__(self, dep: str, reintentar_en: float):
        super().__init__(f"Circuito de {dep} abierto")
        self.dep = dep
        self.reintentar_en = reintentar_en


class BulkheadLleno(Exception):
    """Ya hay el máximo de llamadas en vuelo hacia la dependencia."""

    def __init__(self, dep: str):
        super().__init__(f"Bulkhead de {dep} lleno")
        self.dep = dep


class CircuitBreaker:
    """CERRADO -> ABIERTO -> HALF_OPEN -> CERRADO.

    - CERRADO: todo pasa. Se anotan los resultados de los últimos `ventana_s` segundos. Se abre
      cuando hay al menos `min_llamadas` y la fracción de fallos llega a `umbral_fallos`.
    - ABIERTO: se rechaza todo sin llamar, durante `espera_s`.
    - HALF_OPEN: pasa una sola llamada de prueba a la vez. Con `exitos_para_cerrar` éxitos
      seguidos se cierra; cualquier fallo vuelve a abrir.
    """

    def __init__(self, dep: str, *, umbral_fallos: float = 0.5, min_llamadas: int = 5, ventana_s: float = 10.0,
                 espera_s: float = 5.0, exitos_para_cerrar: int = 2,
                 reloj: Callable[[], float] = time.monotonic):
        self.dep = dep
        self.umbral_fallos = umbral_fallos
        self.min_llamadas = min_llamadas
        self.ventana_s = ventana_s
        self.espera_s = espera_s
        self.exitos_para_cerrar = exitos_para_cerrar
        self._reloj = reloj
        self._lock = threading.Lock()
        self._estado = CERRADO
        self._resultados: deque[tuple[float, bool]] = deque()  # (instante, fue_exito)
        self._abierto_desde = 0.0
        self._sonda_en_curso = False
        self._exitos_half_open = 0
        CIRCUIT_STATE.labels(dep).set(VALOR_ESTADO[CERRADO])

    @property
    def estado(self) -> str:
        with self._lock:
            self._avanzar_si_corresponde()
            return self._estado

    def segundos_para_reintentar(self) -> float:
        with self._lock:
            if self._estado != ABIERTO:
                return 0.0
            return max(0.0, self._abierto_desde + self.espera_s - self._reloj())

    # ------------------------------------------------------------ Uso desde el cliente

    def antes_de_llamar(self) -> None:
        """Pide permiso para llamar. Lanza CircuitoAbierto si no corresponde."""
        with self._lock:
            self._avanzar_si_corresponde()
            if self._estado == ABIERTO:
                CIRCUIT_RECHAZOS.labels(self.dep).inc()
                raise CircuitoAbierto(self.dep, max(0.0, self._abierto_desde + self.espera_s - self._reloj()))
            if self._estado == HALF_OPEN:
                if self._sonda_en_curso:
                    CIRCUIT_RECHAZOS.labels(self.dep).inc()
                    raise CircuitoAbierto(self.dep, 1.0)
                self._sonda_en_curso = True

    def cancelar(self) -> None:
        """La llamada no llegó a hacerse (p. ej. bulkhead lleno): se devuelve el permiso sin contar nada."""
        with self._lock:
            self._sonda_en_curso = False

    def registrar_exito(self) -> None:
        with self._lock:
            if self._estado == HALF_OPEN:
                self._sonda_en_curso = False
                self._exitos_half_open += 1
                if self._exitos_half_open >= self.exitos_para_cerrar:
                    self._resultados.clear()
                    self._cambiar(CERRADO)
            elif self._estado == CERRADO:
                self._anotar(True)

    def registrar_fallo(self) -> None:
        with self._lock:
            if self._estado == HALF_OPEN:
                self._sonda_en_curso = False
                self._abrir()
            elif self._estado == CERRADO:
                self._anotar(False)
                total = len(self._resultados)
                fallos = sum(1 for _, exito in self._resultados if not exito)
                if total >= self.min_llamadas and fallos / total >= self.umbral_fallos:
                    self._abrir()
            # Con el circuito ABIERTO (llamadas que ya estaban en vuelo al abrir) no se hace nada.

    # ------------------------------------------------------------ Internos (con el lock tomado)

    def _anotar(self, exito: bool) -> None:
        ahora = self._reloj()
        self._resultados.append((ahora, exito))
        while self._resultados and self._resultados[0][0] < ahora - self.ventana_s:
            self._resultados.popleft()

    def _abrir(self) -> None:
        self._abierto_desde = self._reloj()
        self._cambiar(ABIERTO)

    def _avanzar_si_corresponde(self) -> None:
        if self._estado == ABIERTO and self._reloj() - self._abierto_desde >= self.espera_s:
            self._sonda_en_curso = False
            self._exitos_half_open = 0
            self._cambiar(HALF_OPEN)

    def _cambiar(self, nuevo: str) -> None:
        anterior, self._estado = self._estado, nuevo
        CIRCUIT_STATE.labels(self.dep).set(VALOR_ESTADO[nuevo])
        CIRCUIT_TRANSICIONES.labels(self.dep, nuevo).inc()
        log.warning("circuito cambia de estado", extra={"dep": self.dep, "de": anterior, "hacia": nuevo})


class Bulkhead:
    """Máximo de llamadas simultáneas hacia una dependencia. El exceso se rechaza al instante.

    Es un semáforo con adquisición NO bloqueante: esperar un lugar libre dejaría el hilo colgado,
    que es justo lo que el bulkhead quiere evitar.
    """

    def __init__(self, dep: str, maximo: int = 5):
        self.dep = dep
        self.maximo = maximo
        self._semaforo = threading.BoundedSemaphore(maximo)
        self._lock = threading.Lock()
        self._en_vuelo = 0
        BULKHEAD_EN_VUELO.labels(dep).set(0)

    @property
    def en_vuelo(self) -> int:
        return self._en_vuelo

    def adquirir(self) -> None:
        if not self._semaforo.acquire(blocking=False):
            BULKHEAD_RECHAZOS.labels(self.dep).inc()
            raise BulkheadLleno(self.dep)
        with self._lock:
            self._en_vuelo += 1
            BULKHEAD_EN_VUELO.labels(self.dep).set(self._en_vuelo)

    def liberar(self) -> None:
        with self._lock:
            self._en_vuelo -= 1
            BULKHEAD_EN_VUELO.labels(self.dep).set(self._en_vuelo)
        self._semaforo.release()
