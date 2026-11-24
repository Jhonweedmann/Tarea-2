"""Reproduce los escenarios de la Saga contra el sistema levantado con `docker compose up`.

Uso (desde la raíz del repo, con las dependencias de requirements-dev.txt):

    python scripts/escenarios.py 1            # éxito: los tres pasos quedan firmes
    python scripts/escenarios.py 2            # falla el paso 3 y se compensa en orden inverso
    python scripts/escenarios.py idempotencia # la misma Idempotency-Key dos veces cobra una sola vez
    python scripts/escenarios.py sin-cupo     # sin cupo: falla el paso 1 y no se cobra
    python scripts/escenarios.py pendiente    # una compensación falla y el worker la termina

Todo pasa por el gateway (http://localhost:8080). Cada corrida crea un estudiante nuevo, así
se puede repetir sin chocar con la regla "una inscripción activa por curso". Las fallas se
inyectan con /admin/falla de Inscripción, que no es pública: se llama dentro del contenedor con
`docker compose exec`.
"""
import argparse
import json
import subprocess
import sys
import time
import uuid

import httpx

GATEWAY = "http://localhost:8080/api/v1"
TOKEN = {"Authorization": "Bearer desarrollo-seguro"}
ESTUDIANTE = "est-001"  # se reemplaza por un estudiante nuevo al empezar cada corrida
# El script de configurar fallas corre dentro del contenedor (allí no hay httpx, se usa urllib).
_FALLA = ("import sys, urllib.request as u; r = u.Request('http://localhost:8000/admin/falla', "
          "data=sys.argv[1].encode(), headers={'Content-Type': 'application/json'}); "
          "print(u.urlopen(r).read().decode())")


def titulo(texto: str) -> None:
    print(f"\n=== {texto} ===")


def mostrar(datos) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2))


def falla_inscripcion(**config) -> None:
    salida = subprocess.run(["docker", "compose", "exec", "-T", "inscripcion", "python", "-c", _FALLA,
                             json.dumps(config)], capture_output=True, text=True, check=True)
    print(f"Inscripción /admin/falla -> {salida.stdout.strip()}")


def matricular(http: httpx.Client, curso_id: str, clave: str | None = None) -> httpx.Response:
    cabeceras = {"X-Correlation-Id": f"demo-{uuid.uuid4().hex[:8]}"}
    if clave:
        cabeceras["Idempotency-Key"] = clave
    titulo(f"POST /api/v1/matriculas  estudiante={ESTUDIANTE} curso={curso_id}"
           + (f"  Idempotency-Key={clave}" if clave else ""))
    r = http.post(f"{GATEWAY}/matriculas", json={"estudiante_id": ESTUDIANTE, "curso_id": curso_id},
                  headers=cabeceras)
    print(f"HTTP {r.status_code}  X-Correlation-Id={r.headers.get('x-correlation-id')}"
          + (f"  Retry-After={r.headers['retry-after']}" if "retry-after" in r.headers else "")
          + (f"  Idempotent-Replay={r.headers['idempotent-replay']}" if "idempotent-replay" in r.headers else ""))
    return r


def historial(http: httpx.Client, saga_id: str) -> dict:
    saga = http.get(f"{GATEWAY}/sagas/{saga_id}").json()
    titulo(f"GET /api/v1/sagas/{saga_id}  estado={saga['estado']}")
    for paso in saga["pasos"]:
        detalle = f"  ({paso['detalle']})" if "detalle" in paso else ""
        print(f"  {paso['ts'][11:23]}  {paso['paso']:<20} {paso['resultado']}{detalle}")
    return saga


def estado_final(http: httpx.Client, saga_id: str, curso_id: str) -> dict:
    """Lo que quedó en las tres BD para esta saga, leído del panel del BFF."""
    panel = http.get(f"{GATEWAY}/web/alumnos/{ESTUDIANTE}/panel").json()
    inscripcion = next((i["estado"] for i in panel["inscripciones"].get("items", [])
                        if i["inscripcion_id"] == saga_id), "no existe")
    cobro = next((c["estado"] for c in panel["pagos"].get("items", []) if c["cobro_id"] == saga_id), "no existe")
    cupos = next((c["cupos_disponibles"] for c in panel["cursos"].get("items", []) if c["id"] == curso_id), None)
    resultado = {"inscripcion": inscripcion, "cobro": cobro, f"cupos_disponibles_{curso_id}": cupos}
    titulo("Estado final en Inscripción, Pagos y Cupos (vía panel del BFF)")
    mostrar(resultado)
    return resultado


def crear_estudiante(http: httpx.Client) -> str:
    sufijo = uuid.uuid4().hex[:6]
    r = http.post(f"{GATEWAY}/estudiantes", json={"nombre": f"Demo {sufijo}", "email": f"demo-{sufijo}@example.com"})
    r.raise_for_status()
    print(f"Estudiante de prueba: {r.json()['id']}")
    return r.json()["id"]


def cupos_disponibles(http: httpx.Client, curso_id: str) -> int:
    return http.get(f"{GATEWAY}/cursos/{curso_id}").json()["cupos_disponibles"]


def curso_con_cupo(http: httpx.Client) -> str:
    cursos = http.get(f"{GATEWAY}/cursos").json()["items"]
    disponibles = [c for c in cursos if c["cupos_disponibles"] > 0]
    if not disponibles:
        sys.exit("No quedan cursos con cupo. Reinicie los datos: docker compose down -v && docker compose up -d")
    return max(disponibles, key=lambda c: c["cupos_disponibles"])["id"]


# ---------------------------------------------------------------- Escenarios

def escenario_1(http: httpx.Client) -> None:
    curso = curso_con_cupo(http)
    antes = cupos_disponibles(http, curso)
    r = matricular(http, curso)
    saga = historial(http, r.json()["saga_id"])
    final = estado_final(http, saga["saga_id"], curso)
    ok = (r.status_code == 201 and saga["estado"] == "COMPLETADA" and final["inscripcion"] == "activa"
          and final["cobro"] == "cobrado" and final[f"cupos_disponibles_{curso}"] == antes - 1)
    print(f"\nResultado: {'OK' if ok else 'INESPERADO'} (cupos {antes} -> {final[f'cupos_disponibles_{curso}']})")


def escenario_2(http: httpx.Client) -> None:
    curso = curso_con_cupo(http)
    antes = cupos_disponibles(http, curso)
    titulo("Se hace fallar el ÚLTIMO paso: Inscripción responde 503")
    falla_inscripcion(caido=True)
    try:
        r = matricular(http, curso)
        mostrar(r.json())
    finally:
        falla_inscripcion(caido=False)
    saga = historial(http, r.json()["saga_id"])
    final = estado_final(http, saga["saga_id"], curso)
    ok = (saga["estado"] == "COMPENSADA" and final["cobro"] == "reembolsado"
          and final["inscripcion"] != "activa" and final[f"cupos_disponibles_{curso}"] == antes)
    print(f"\nResultado: {'OK' if ok else 'INESPERADO'}: reembolso y luego liberar cupo (orden inverso); "
          f"cupos {antes} -> {final[f'cupos_disponibles_{curso}']}, sin cobro ni reserva colgados")


def escenario_idempotencia(http: httpx.Client) -> None:
    curso = curso_con_cupo(http)
    antes = cupos_disponibles(http, curso)
    clave = f"demo-{uuid.uuid4().hex[:8]}"
    primera = matricular(http, curso, clave)
    segunda = matricular(http, curso, clave)
    misma = primera.json()["saga_id"] == segunda.json()["saga_id"]
    despues = cupos_disponibles(http, curso)
    ok = (primera.status_code, segunda.status_code) == (201, 200) and misma and despues == antes - 1
    print(f"\nResultado: {'OK' if ok else 'INESPERADO'}: misma saga={misma}, cupos {antes} -> {despues} "
          "(un solo cupo y un solo cobro)")


def escenario_sin_cupo(http: httpx.Client) -> None:
    cursos = http.get(f"{GATEWAY}/cursos").json()["items"]
    curso = min(cursos, key=lambda c: c["cupos_disponibles"])
    titulo(f"Se agotan los cupos de {curso['id']} ({curso['cupos_disponibles']} disponibles)")
    for _ in range(curso["cupos_disponibles"]):
        matricular(http, curso["id"])
    r = matricular(http, curso["id"])
    mostrar(r.json())
    saga = historial(http, r.json()["saga_id"])
    final = estado_final(http, saga["saga_id"], curso["id"])
    ok = r.status_code == 409 and saga["estado"] == "ABORTADA_SIN_CUPO" and final["cobro"] == "no existe"
    print(f"\nResultado: {'OK' if ok else 'INESPERADO'}: falla en el paso 1 y no se cobra")


def escenario_pendiente(http: httpx.Client) -> None:
    curso = curso_con_cupo(http)
    titulo("Inscripción lenta (3 s > timeout de 2 s): inscribir y dar de baja quedan inciertos")
    falla_inscripcion(latencia_ms=3000)
    try:
        r = matricular(http, curso)
        saga_id = r.json()["saga_id"]
        historial(http, saga_id)
    finally:
        titulo("Inscripción se recupera; el worker reintenta con backoff")
        falla_inscripcion(latencia_ms=0)
    for _ in range(30):
        time.sleep(1)
        estado = http.get(f"{GATEWAY}/sagas/{saga_id}").json()["estado"]
        print(f"  estado: {estado}")
        if estado != "COMPENSACION_PENDIENTE":
            break
    historial(http, saga_id)
    estado_final(http, saga_id, curso)


ESCENARIOS = {"1": escenario_1, "2": escenario_2, "idempotencia": escenario_idempotencia,
              "sin-cupo": escenario_sin_cupo, "pendiente": escenario_pendiente}


def main() -> None:
    global ESTUDIANTE
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("escenario", choices=ESCENARIOS)
    args = parser.parse_args()
    with httpx.Client(headers=TOKEN, timeout=20) as http:
        ESTUDIANTE = crear_estudiante(http)
        ESCENARIOS[args.escenario](http)


if __name__ == "__main__":
    main()
