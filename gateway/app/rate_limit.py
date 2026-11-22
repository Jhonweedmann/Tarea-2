"""Rate limit por cliente con ventana fija en Redis (INCR + EXPIRE)."""
import logging
import math
import time
from dataclasses import dataclass

import redis

log = logging.getLogger("gateway")


@dataclass
class Resultado:
    permitido: bool
    limite: int
    restantes: int
    reintentar_en: int  # segundos hasta que empieza la siguiente ventana


class LimitadorVentanaFija:
    """Cuenta solicitudes por cliente en ventanas de `ventana_s` segundos.

    La clave incluye el número de ventana (`ratelimit:<cliente>:<n>`), así que al empezar una
    ventana nueva la cuenta parte de cero sin borrar nada; el EXPIRE solo limpia las claves viejas.
    Al estar en Redis, el límite se respeta aunque haya varias réplicas del gateway.
    """

    def __init__(self, cliente_redis, ventana_s: int, pausa_s: float = 15, reloj=time.time):
        self.redis = cliente_redis
        self.ventana_s = ventana_s
        self.pausa_s = pausa_s
        self.reloj = reloj
        self._pausa_hasta = 0.0

    async def consumir(self, cliente: str, limite: int) -> Resultado | None:
        """Suma una solicitud. Devuelve None si Redis no está disponible (el llamador decide)."""
        if time.monotonic() < self._pausa_hasta:
            return None
        ahora = self.reloj()
        ventana = int(ahora // self.ventana_s)
        clave = f"ratelimit:{cliente}:{ventana}"
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.incr(clave)
                pipe.expire(clave, self.ventana_s + 1)
                cuenta, _ = await pipe.execute()
        except (redis.RedisError, OSError) as error:
            # Con Redis caído, resolver su nombre puede tardar segundos; se deja de intentar un rato
            # para no sumar esa espera a cada solicitud (mismo criterio que en la Tarea 1).
            self._pausa_hasta = time.monotonic() + self.pausa_s
            log.warning("rate limit sin Redis; se deja pasar la solicitud", extra={"error": repr(error)})
            return None
        reintentar = max(1, math.ceil((ventana + 1) * self.ventana_s - ahora))
        return Resultado(cuenta <= limite, limite, max(0, limite - cuenta), reintentar)
