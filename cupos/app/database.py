import sqlite3


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS cursos (
            id TEXT PRIMARY KEY, nombre TEXT NOT NULL,
            cupos_totales INTEGER NOT NULL CHECK(cupos_totales >= 0),
            cupos_disponibles INTEGER NOT NULL CHECK(cupos_disponibles >= 0)
        )"""
    )
    # Una fila por reserva_id: la clave primaria es lo que impide descontar dos veces
    # el mismo cupo si el orquestador repite ReservarCupo. curso_id queda NULL en una
    # "lapida" (LiberarReserva de una reserva que nunca llego a existir).
    db.execute(
        """CREATE TABLE IF NOT EXISTS reservas (
            reserva_id TEXT PRIMARY KEY,
            curso_id TEXT REFERENCES cursos(id),
            estado TEXT NOT NULL CHECK(estado IN ('reservada', 'liberada')),
            creada_en TEXT NOT NULL,
            actualizada_en TEXT NOT NULL
        )"""
    )
    return db


def seed(db: sqlite3.Connection) -> None:
    # INSERT OR IGNORE: agrega los cursos que falten sin tocar los cupos de los que ya existen,
    # asi un volumen creado antes tambien recibe los cursos nuevos.
    db.executemany(
        "INSERT OR IGNORE INTO cursos VALUES (?, ?, ?, ?)",
        [("ARQ-101", "Arquitectura de Sistemas", 2, 2),
         ("API-201", "Integracion de APIs", 1, 1),
         ("DAT-110", "Fundamentos de Datos", 3, 3),
         # Curso con muchos cupos para repetir los escenarios de la Saga en la demo.
         ("INT-301", "Integracion de Sistemas", 30, 30)],
    )
    db.commit()
