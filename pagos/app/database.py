import sqlite3

from datetime import datetime, timezone


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(
        """-- Paso 2 de la Saga. cobro_id es el saga_id: la clave primaria impide cobrar dos veces si el
        -- orquestador repite la llamada. Una "lapida" es un reembolso de un cobro que nunca llego a
        -- crearse (timeout ambiguo): estudiante_id, curso_id y monto quedan NULL y un POST /cobros
        -- atrasado con ese id se rechaza con 409.
        CREATE TABLE IF NOT EXISTS cobros (
            cobro_id TEXT PRIMARY KEY,
            estudiante_id TEXT,
            curso_id TEXT,
            monto INTEGER CHECK(monto IS NULL OR monto > 0),
            estado TEXT NOT NULL CHECK(estado IN ('cobrado', 'reembolsado')),
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cobros_por_estudiante ON cobros(estudiante_id);"""
    )
    db.commit()
    return db


def seed(db: sqlite3.Connection) -> None:
    """Cobro de demostración para est-001 (el mismo que traía el stub), así el panel del BFF tiene datos."""
    marca = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).isoformat()
    db.execute("INSERT OR IGNORE INTO cobros VALUES ('insc-semilla-001', 'est-001', 'ARQ-101', 150000, 'cobrado', ?, ?)",
               (marca, marca))
    db.commit()
