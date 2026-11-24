"""Log de la Saga en la BD propia del orquestador.

Cada saga es una fila en `sagas` (estado actual) y cada paso o compensación ejecutado es una fila
en `pasos_saga` (historial). Guardar cada transición antes de seguir permite:
- saber qué compensar si algo falla (se deduce de los pasos registrados),
- retomar una saga si el orquestador se reinicia a mitad de camino,
- mostrar el historial en GET /sagas/{id}, que es la evidencia de los escenarios 1 y 2.
"""
import sqlite3
import threading
from datetime import datetime, timezone

ESTADOS = (
    "INICIADA", "CUPO_RESERVADO", "PAGO_COBRADO", "COMPLETADA",
    "COMPENSANDO", "COMPENSADA", "COMPENSACION_PENDIENTE",
    "ABORTADA_SIN_CUPO", "ABORTADA_PAGOS_NO_DISPONIBLE", "ABORTADA_NO_ENCONTRADO",
)
# Estados en los que la saga ya terminó y no se vuelve a tocar.
ESTADOS_FINALES = {"COMPLETADA", "COMPENSADA", "ABORTADA_SIN_CUPO", "ABORTADA_PAGOS_NO_DISPONIBLE",
                   "ABORTADA_NO_ENCONTRADO"}
# Resultado de un paso: ok (se aplicó), error (seguro que no se aplicó) o incierto (pudo aplicarse:
# timeout o conexión cortada después de enviar). Un paso incierto se compensa por si acaso.
RESULTADOS = ("ok", "error", "incierto")
# Columnas de `sagas` que se pueden cambiar junto con el estado.
CAMPOS_EDITABLES = {"estado_tras_compensar", "error", "intentos_compensacion", "proximo_intento"}


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    estados = ", ".join(f"'{estado}'" for estado in ESTADOS)
    resultados = ", ".join(f"'{resultado}'" for resultado in RESULTADOS)
    db.executescript(
        f"""CREATE TABLE IF NOT EXISTS sagas (
            saga_id TEXT PRIMARY KEY,
            estado TEXT NOT NULL CHECK(estado IN ({estados})),
            estudiante_id TEXT NOT NULL,
            curso_id TEXT NOT NULL,
            idempotency_key TEXT,
            -- Estado al que pasa la saga cuando terminen sus compensaciones
            -- (COMPENSADA o el ABORTADA_* que explica por qué se deshizo).
            estado_tras_compensar TEXT,
            error TEXT,
            intentos_compensacion INTEGER NOT NULL DEFAULT 0,
            proximo_intento TEXT,
            creada_en TEXT NOT NULL,
            actualizada_en TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sagas_por_estado ON sagas(estado);
        CREATE TABLE IF NOT EXISTS pasos_saga (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
            paso TEXT NOT NULL,
            resultado TEXT NOT NULL CHECK(resultado IN ({resultados})),
            detalle TEXT,
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pasos_por_saga ON pasos_saga(saga_id, id);"""
    )
    db.commit()
    return db


class RepositorioSagas:
    """Acceso a las tablas de la saga. Un lock serializa las escrituras entre hilos
    (las peticiones HTTP y el worker de compensaciones comparten la conexión)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.lock = threading.Lock()

    def crear(self, saga_id: str, estudiante_id: str, curso_id: str, idempotency_key: str | None = None) -> None:
        marca = ahora()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO sagas (saga_id, estado, estudiante_id, curso_id, idempotency_key, creada_en,"
                " actualizada_en) VALUES (?, 'INICIADA', ?, ?, ?, ?, ?)",
                (saga_id, estudiante_id, curso_id, idempotency_key, marca, marca),
            )

    def cambiar_estado(self, saga_id: str, estado: str, **campos) -> None:
        desconocidos = set(campos) - CAMPOS_EDITABLES
        if desconocidos:
            raise ValueError(f"Campos no editables: {desconocidos}")
        asignaciones = "".join(f", {campo} = ?" for campo in campos)
        with self.lock, self.db:
            self.db.execute(
                f"UPDATE sagas SET estado = ?, actualizada_en = ?{asignaciones} WHERE saga_id = ?",
                (estado, ahora(), *campos.values(), saga_id),
            )

    def registrar_paso(self, saga_id: str, paso: str, resultado: str, detalle: str | None = None) -> None:
        marca = ahora()
        with self.lock, self.db:
            self.db.execute("INSERT INTO pasos_saga (saga_id, paso, resultado, detalle, ts) VALUES (?, ?, ?, ?, ?)",
                            (saga_id, paso, resultado, detalle, marca))
            self.db.execute("UPDATE sagas SET actualizada_en = ? WHERE saga_id = ?", (marca, saga_id))

    def obtener(self, saga_id: str) -> dict | None:
        with self.lock:
            fila = self.db.execute("SELECT * FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        return dict(fila) if fila else None

    def pendientes(self, hasta: str) -> list[str]:
        """Sagas en COMPENSACION_PENDIENTE cuyo próximo intento ya llegó (o que aún no tienen uno)."""
        with self.lock:
            filas = self.db.execute(
                "SELECT saga_id FROM sagas WHERE estado = 'COMPENSACION_PENDIENTE'"
                " AND (proximo_intento IS NULL OR proximo_intento <= ?) ORDER BY actualizada_en", (hasta,)
            ).fetchall()
        return [fila["saga_id"] for fila in filas]

    def estancadas(self, sin_cambios_desde: str) -> list[dict]:
        """Sagas a medio camino que no avanzan desde hace rato: el orquestador se cayó mientras corrían."""
        with self.lock:
            filas = self.db.execute(
                "SELECT * FROM sagas WHERE estado IN ('INICIADA', 'CUPO_RESERVADO', 'PAGO_COBRADO', 'COMPENSANDO')"
                " AND actualizada_en < ? ORDER BY actualizada_en", (sin_cambios_desde,)
            ).fetchall()
        return [dict(fila) for fila in filas]

    def pasos(self, saga_id: str) -> list[dict]:
        with self.lock:
            filas = self.db.execute(
                "SELECT paso, resultado, detalle, ts FROM pasos_saga WHERE saga_id = ? ORDER BY id", (saga_id,)
            ).fetchall()
        return [dict(fila) for fila in filas]
