import sqlite3


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(
        """CREATE TABLE IF NOT EXISTS estudiantes (
            id TEXT PRIMARY KEY, nombre TEXT NOT NULL, email TEXT NOT NULL UNIQUE
        );
        -- Paso 3 de la Saga. inscripcion_id es el saga_id: la clave primaria impide crear dos
        -- inscripciones si el orquestador repite la llamada. En una "lapida" (baja de una
        -- inscripcion que nunca llego a crearse) estudiante_id y curso_id quedan NULL.
        CREATE TABLE IF NOT EXISTS inscripciones (
            inscripcion_id TEXT PRIMARY KEY,
            estudiante_id TEXT REFERENCES estudiantes(id),
            curso_id TEXT,
            estado TEXT NOT NULL CHECK(estado IN ('activa', 'baja')),
            creada_en TEXT NOT NULL,
            actualizada_en TEXT NOT NULL
        );
        -- Regla de negocio: un estudiante no puede tener dos inscripciones activas en el mismo curso.
        CREATE UNIQUE INDEX IF NOT EXISTS una_inscripcion_activa_por_curso
            ON inscripciones(estudiante_id, curso_id) WHERE estado = 'activa';"""
    )
    db.commit()
    return db
