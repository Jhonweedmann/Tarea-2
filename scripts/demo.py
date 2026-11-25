"""Apoyo para el video: comandos cortos para el escenario 3 (Pagos lento / caído) sin escribir curls largos.

Uso, desde la raíz del repo y con el sistema levantado (docker compose up -d):

    python scripts/demo.py falla lento        # Pagos tarda 5 s por llamada
    python scripts/demo.py falla caido        # Pagos responde 503
    python scripts/demo.py falla sano         # Pagos vuelve a la normalidad
    python scripts/demo.py matricular         # UNA matrícula con alumno nuevo: código, tiempo, Retry-After, saga
    python scripts/demo.py matricular --pagos-roto   # lo mismo con est-001 (no llega a inscribir: no consume cupos)
    python scripts/demo.py rafaga --n 25      # 25 matrículas simultáneas (menos que los 30 cupos de INT-301) + una sonda de GET /cursos al lado
    python scripts/demo.py metricas           # circuit_state, bulkhead y llamadas a Pagos (del orquestador)

Todo pasa por el gateway (http://localhost:8080). Las fallas y las métricas internas se piden dentro de los
contenedores con `docker compose exec`, igual que en scripts/escenarios.py (con --sin-docker, por HTTP local).
"""
import argparse
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from experimentos.experimento import Entorno, clasificar, parsear  # noqa: E402

GATEWAY = "http://localhost:8080/api/v1"
FALLAS = {"lento": {"latencia_ms": 5000, "caido": False}, "caido": {"latencia_ms": 0, "caido": True},
          "sano": {"latencia_ms": 0, "caido": False, "tasa_error": 0.0}}
METRICAS = ("circuit_state", "bulkhead_in_flight", "bulkhead_rejected_total", "circuit_rejected_total",
            "pagos_llamadas_total", "pagos_reintentos_total")


def cliente(token: str) -> httpx.Client:
    return httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30.0),
                        limits=httpx.Limits(max_connections=200))


def alumno_nuevo(http: httpx.Client) -> str:
    sufijo = uuid.uuid4().hex[:8]
    r = http.post(f"{GATEWAY}/estudiantes", json={"nombre": f"Demo {sufijo}", "email": f"demo-{sufijo}@example.com"})
    r.raise_for_status()
    return r.json()["id"]


def una_matricula(http: httpx.Client, estudiante: str, curso: str) -> tuple[int, str, float, httpx.Response | None]:
    inicio = time.perf_counter()
    try:
        r = http.post(f"{GATEWAY}/matriculas", json={"estudiante_id": estudiante, "curso_id": curso},
                      headers={"X-Correlation-Id": f"demo-{uuid.uuid4().hex[:8]}"})
    except httpx.HTTPError:
        return 0, "sin_respuesta", (time.perf_counter() - inicio) * 1000, None
    return r.status_code, clasificar(r)[0], (time.perf_counter() - inicio) * 1000, r


def cmd_matricular(args, entorno: Entorno) -> None:
    http = cliente("desarrollo-seguro")
    estudiante = "est-001" if args.pagos_roto else alumno_nuevo(http)
    codigo, motivo, ms, r = una_matricula(http, estudiante, args.curso)
    print(f"POST /matriculas ({estudiante}, {args.curso}) -> {codigo} ({motivo}) en {ms:,.0f} ms")
    if r is not None:
        if "retry-after" in r.headers:
            print(f"  Retry-After: {r.headers['retry-after']} s")
        cuerpo = r.json()
        print(f"  saga_id: {cuerpo.get('saga_id')}  estado: {cuerpo.get('estado')}")
        if cuerpo.get("detail"):
            print(f"  detalle: {cuerpo['detail']}")


def cmd_rafaga(args, entorno: Entorno) -> None:
    http = cliente("carga-experimentos")  # límite de 100 000: el rate limit del gateway no interfiere
    resultados, sonda, parar = [], [], threading.Event()

    def sondear():
        while not parar.is_set():
            inicio = time.perf_counter()
            try:
                codigo = http.get(f"{GATEWAY}/cursos").status_code
            except httpx.HTTPError:
                codigo = 0
            sonda.append((codigo, (time.perf_counter() - inicio) * 1000))
            parar.wait(0.3)

    hilo = threading.Thread(target=sondear, daemon=True)
    hilo.start()
    # Un alumno distinto por matrícula (así el orquestador no las descarta como duplicadas en curso).
    alumnos = [alumno_nuevo(http) for _ in range(args.n)]  # en serie: altas simultáneas pueden dar 500 en Inscripción
    print(f"Lanzando {args.n} matrículas simultáneas de {args.n} alumnos contra {args.curso}...")
    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.n) as pool:
        resultados = list(pool.map(lambda a: una_matricula(http, a, args.curso)[:3], alumnos))
    total = time.perf_counter() - inicio
    parar.set()
    hilo.join()
    conteo: dict[str, int] = {}
    for codigo, motivo, _ in resultados:
        conteo[f"{codigo} {motivo}"] = conteo.get(f"{codigo} {motivo}", 0) + 1
    tiempos = sorted(ms for _, _, ms in resultados)
    print(f"\nTerminó todo en {total:.1f} s")
    print("Resultados:", dict(sorted(conteo.items(), key=lambda kv: -kv[1])))
    print(f"Matrículas: p50={statistics.median(tiempos):,.0f} ms  p95={tiempos[int(len(tiempos) * .95) - 1]:,.0f} ms  máx={tiempos[-1]:,.0f} ms"
          f"")
    if sonda:
        ms_sonda = [ms for _, ms in sonda]
        print(f"Sonda GET /cursos durante la ráfaga (¿el resto del sistema sufre?): {len(sonda)} consultas, "
              f"máx={max(ms_sonda):,.0f} ms, errores={sum(c != 200 for c, _ in sonda)}")


def cmd_falla(args, entorno: Entorno) -> None:
    entorno.inyectar_falla(**FALLAS[args.modo])
    print(f"Pagos -> {args.modo}: {FALLAS[args.modo]}")


def cmd_metricas(args, entorno: Entorno) -> None:
    filas = parsear(entorno.metricas("orquestador"))
    nombres = {0: "CERRADO", 1: "HALF-OPEN", 2: "ABIERTO"}
    for nombre, etiquetas, valor in filas:
        if nombre not in METRICAS:
            continue
        extra = f" ({nombres[int(valor)]})" if nombre == "circuit_state" else ""
        etiq = ",".join(f"{k}={v}" for k, v in etiquetas.items())
        print(f"{nombre}{{{etiq}}} = {valor:g}{extra}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sin-docker", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("falla"); f.add_argument("modo", choices=list(FALLAS)); f.set_defaults(fn=cmd_falla)
    m = sub.add_parser("matricular"); m.set_defaults(fn=cmd_matricular)
    m.add_argument("--curso", default="INT-301"); m.add_argument("--pagos-roto", action="store_true")
    r = sub.add_parser("rafaga"); r.set_defaults(fn=cmd_rafaga)
    r.add_argument("--n", type=int, default=25); r.add_argument("--curso", default="INT-301")
    sub.add_parser("metricas").set_defaults(fn=cmd_metricas)
    args = ap.parse_args()
    args.fn(args, Entorno(con_docker=not args.sin_docker))


if __name__ == "__main__":
    main()
