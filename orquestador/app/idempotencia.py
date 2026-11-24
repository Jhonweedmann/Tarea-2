"""Idempotency-Key de POST /matriculas, guardada en Redis.

Reutiliza la idea de la Tarea 1: SET NX (solo escribe si la clave no existe) es atómico, así que de
dos reintentos simultáneos con la misma clave solo uno crea la saga. La clave apunta al saga_id;
el estado real de la matrícula se lee de la BD del orquestador.
"""
import redis

from comun.observabilidad import obtener_logger

log = obtener_logger("orquestador")


class IdempotenciaNoDisponible(Exception):
    """Redis no responde: no se puede garantizar que un reintento no cree otra matrícula."""


class Idempotencia:
    PREFIJO = "idempotencia:matricula:"

    def __init__(self, cliente: redis.Redis, ttl_segundos: int = 86400):
        self.redis = cliente
        self.ttl = ttl_segundos

    def reservar(self, clave: str, saga_id: str) -> str | None:
        """Asocia la clave a saga_id. Devuelve None si lo logró, o el saga_id que ya la tenía
        ("" si la clave expiró justo entre las dos operaciones: el llamador vuelve a intentar)."""
        try:
            if self.redis.set(self.PREFIJO + clave, saga_id, nx=True, ex=self.ttl):
                return None
            return self.redis.get(self.PREFIJO + clave) or ""
        except redis.RedisError as error:
            raise IdempotenciaNoDisponible(str(error)) from error

    def liberar(self, clave: str, saga_id: str) -> None:
        """Suelta la clave si sigue apuntando a esta saga. Si Redis falla, la clave expira sola (TTL)."""
        try:
            if self.redis.get(self.PREFIJO + clave) == saga_id:
                self.redis.delete(self.PREFIJO + clave)
        except redis.RedisError as error:
            log.warning("no se pudo liberar la Idempotency-Key", extra={"saga_id": saga_id, "motivo": str(error)})

    def close(self) -> None:
        self.redis.close()
