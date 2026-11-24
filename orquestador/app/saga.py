"""Saga orquestada de matrícula: reservar cupo -> cobrar -> inscribir.

Reglas:
1. Cada paso envía el saga_id como clave idempotente (reserva_id, cobro_id, inscripcion_id), así
   repetir un paso o su compensación nunca descuenta, cobra ni inscribe dos veces.
2. Antes y después de cada paso se guarda el estado en la BD (RepositorioSagas).
3. Si un paso falla, se compensan en ORDEN INVERSO los pasos que se aplicaron o que pudieron
   aplicarse (resultado "ok" o "incierto"). Un paso que falló de forma segura ("error") no se
   compensa porque no dejó nada que deshacer.
4. Si una compensación falla, la saga queda en COMPENSACION_PENDIENTE y el worker la reintenta
   desde ese punto, respetando el mismo orden.
"""
from dataclasses import dataclass

import grpc
from prometheus_client import Counter

from comun.observabilidad import obtener_logger

from .database import RepositorioSagas
from .inscripcion_client import InscripcionError
from .pagos_client import PagosNoDisponible, PagosRechazado

PASOS = ("reservar_cupo", "cobrar", "inscribir")
COMPENSACION = {"reservar_cupo": "liberar_cupo", "cobrar": "reembolsar", "inscribir": "dar_baja"}
ESTADO_TRAS_PASO = {"reservar_cupo": "CUPO_RESERVADO", "cobrar": "PAGO_COBRADO", "inscribir": "COMPLETADA"}

SAGAS_TOTAL = Counter("saga_total", "Sagas que llegaron a un estado final, por resultado", ["resultado"])
log = obtener_logger("orquestador")


@dataclass(eq=False)
class FallaPaso(Exception):
    """Un paso (o la validación previa) no se completó.

    incierto: no se sabe si el servicio aplicó la operación; se compensa por si acaso.
    estado_final: estado de la saga cuando terminen las compensaciones.
    codigo_http / titulo / retry_after: cómo se lo explica el orquestador al cliente.
    """
    motivo: str
    incierto: bool
    codigo_http: int
    titulo: str
    estado_final: str = "COMPENSADA"
    retry_after: int | None = None


class Saga:
    def __init__(self, repo: RepositorioSagas, cupos, pagos, inscripcion, monto: int):
        self.repo = repo
        self.cupos = cupos
        self.pagos = pagos
        self.inscripcion = inscripcion
        self.monto = monto

    # ------------------------------------------------------------ Camino hacia adelante

    def ejecutar(self, saga_id: str) -> FallaPaso | None:
        """Ejecuta la saga ya creada con repo.crear(). Devuelve None si se completó."""
        saga = self.repo.obtener(saga_id)
        try:
            self._validar_estudiante(saga)
        except FallaPaso as falla:
            self._fallar(saga_id, "validar_estudiante", falla)
            return falla

        for paso in PASOS:
            try:
                getattr(self, f"_{paso}")(saga)
            except FallaPaso as falla:
                self._fallar(saga_id, paso, falla)
                return falla
            self.repo.registrar_paso(saga_id, paso, "ok")
            self.repo.cambiar_estado(saga_id, ESTADO_TRAS_PASO[paso])
            log.info("paso completado", extra={"saga_id": saga_id, "paso": paso})

        SAGAS_TOTAL.labels("COMPLETADA").inc()
        log.info("saga completada", extra={"saga_id": saga_id})
        return None

    def _validar_estudiante(self, saga: dict) -> None:
        # Validación temprana para no reservar ni cobrar a un estudiante que no existe. Si
        # Inscripción no responde no se bloquea la matrícula: el paso 3 vuelve a validar.
        try:
            existe = self.inscripcion.estudiante_existe(saga["estudiante_id"])
        except InscripcionError as error:
            self.repo.registrar_paso(saga["saga_id"], "validar_estudiante", "incierto",
                                     f"{error}; se valida en el paso 3")
            return
        if not existe:
            raise FallaPaso("No existe un estudiante con ese id.", incierto=False, codigo_http=404,
                            titulo="Estudiante no encontrado", estado_final="ABORTADA_NO_ENCONTRADO")
        self.repo.registrar_paso(saga["saga_id"], "validar_estudiante", "ok")

    def _reservar_cupo(self, saga: dict) -> None:
        try:
            self.cupos.reservar(saga["saga_id"], saga["curso_id"])
        except grpc.RpcError as error:
            codigo = error.code()
            if codigo == grpc.StatusCode.FAILED_PRECONDITION:
                raise FallaPaso(error.details() or "No hay cupos disponibles", incierto=False, codigo_http=409,
                                titulo="Sin cupo", estado_final="ABORTADA_SIN_CUPO")
            if codigo == grpc.StatusCode.NOT_FOUND:
                raise FallaPaso("No existe un curso con ese id.", incierto=False, codigo_http=404,
                                titulo="Curso no encontrado", estado_final="ABORTADA_NO_ENCONTRADO")
            # DEADLINE_EXCEEDED, UNAVAILABLE u otro: la reserva pudo quedar hecha.
            raise FallaPaso(f"Cupos no respondió ({codigo.name})", incierto=True, codigo_http=503,
                            titulo="Cupos no disponible", retry_after=5)

    def _cobrar(self, saga: dict) -> None:
        try:
            self.pagos.cobrar(saga["saga_id"], saga["estudiante_id"], saga["curso_id"], self.monto)
        except PagosRechazado as error:
            raise FallaPaso(str(error), incierto=False, codigo_http=409, titulo="Pago rechazado")
        except PagosNoDisponible as error:
            raise FallaPaso(str(error), incierto=error.puede_haberse_aplicado, codigo_http=503,
                            titulo="Pagos no disponible", estado_final="ABORTADA_PAGOS_NO_DISPONIBLE",
                            retry_after=error.retry_after)

    def _inscribir(self, saga: dict) -> None:
        try:
            self.inscripcion.inscribir(saga["saga_id"], saga["estudiante_id"], saga["curso_id"])
        except InscripcionError as error:
            if error.status in (404, 409):
                titulo = "Estudiante no encontrado" if error.status == 404 else "Inscripción rechazada"
                raise FallaPaso(str(error), incierto=False, codigo_http=error.status, titulo=titulo)
            raise FallaPaso(str(error), incierto=error.incierta, codigo_http=502, titulo="Matrícula no completada")

    # ------------------------------------------------------------ Compensación

    def _fallar(self, saga_id: str, paso: str, falla: FallaPaso) -> None:
        resultado = "incierto" if falla.incierto else "error"
        self.repo.registrar_paso(saga_id, paso, resultado, falla.motivo)
        log.warning("paso fallido", extra={"saga_id": saga_id, "paso": paso, "resultado": resultado,
                                            "motivo": falla.motivo})
        self.repo.cambiar_estado(saga_id, "COMPENSANDO", estado_tras_compensar=falla.estado_final,
                                 error=falla.motivo)
        self.compensar(saga_id)

    def compensaciones_pendientes(self, saga_id: str) -> list[str]:
        """Pasos que hay que deshacer, en orden inverso, deducidos del historial."""
        pasos = self.repo.pasos(saga_id)
        aplicados = [p["paso"] for p in pasos if p["paso"] in PASOS and p["resultado"] in ("ok", "incierto")]
        compensados = {p["paso"] for p in pasos if p["paso"] in COMPENSACION.values() and p["resultado"] == "ok"}
        return [paso for paso in reversed(aplicados) if COMPENSACION[paso] not in compensados]

    def compensar(self, saga_id: str) -> bool:
        """Deshace en orden inverso lo pendiente. Devuelve True si la saga quedó en un estado final."""
        saga = self.repo.obtener(saga_id)
        for paso in self.compensaciones_pendientes(saga_id):
            compensacion = COMPENSACION[paso]
            try:
                self._ejecutar_compensacion(compensacion, saga)
            except Exception as error:  # cualquier falla deja la saga pendiente para el worker
                self.repo.registrar_paso(saga_id, compensacion, "error", str(error) or type(error).__name__)
                self.repo.cambiar_estado(saga_id, "COMPENSACION_PENDIENTE")
                log.warning("compensación fallida, queda pendiente",
                            extra={"saga_id": saga_id, "paso": compensacion, "motivo": str(error)})
                return False
            self.repo.registrar_paso(saga_id, compensacion, "ok")
            log.info("compensación aplicada", extra={"saga_id": saga_id, "paso": compensacion})

        estado = saga["estado_tras_compensar"] or "COMPENSADA"
        self.repo.cambiar_estado(saga_id, estado, proximo_intento=None)
        SAGAS_TOTAL.labels(estado).inc()
        log.info("saga terminada sin matrícula", extra={"saga_id": saga_id, "estado": estado})
        return True

    def _ejecutar_compensacion(self, compensacion: str, saga: dict) -> None:
        saga_id = saga["saga_id"]
        if compensacion == "dar_baja":
            self.inscripcion.dar_baja(saga_id)
        elif compensacion == "reembolsar":
            self.pagos.reembolsar(saga_id)
        elif compensacion == "liberar_cupo":
            self.cupos.liberar(saga_id)
        else:
            raise ValueError(f"Compensación desconocida: {compensacion}")
