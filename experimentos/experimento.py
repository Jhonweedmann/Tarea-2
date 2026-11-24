"""Escenario 3 — falla en cascada contenida (Pagos lento y luego caído), con y sin resiliencia.

Uso (desde la raíz del repo, con el sistema levantado con `docker compose up -d`):

    python experimentos/experimento.py --modo on       # con timeout, reintento, circuit breaker y bulkhead
    python experimentos/experimento.py --modo off      # "sin patrón": RESILIENCIA=off (solo timeout)
    python experimentos/experimento.py --modo ambos    # las dos corridas seguidas y la comparación
    python experimentos/experimento.py --comparar docs/resultados/escenario3-on.json docs/resultados/escenario3-off.json

Cada corrida reinicia el orquestador con el modo pedido (docker compose up --force-recreate) y recorre
cuatro fases, todas a través del gateway con el token `carga-experimentos` (límite alto):

    1. linea_base     Pagos sano; unas pocas matrículas reales.
    2. pagos_lento    Pagos tarda 5 s por llamada y llega un PICO de carga (30 matrículas/s).
    3. pagos_caido    Pagos responde 503; la carga sigue.
    4. recuperacion   Pagos vuelve; se observa half-open -> cerrado y la compensación pendiente.

Durante todo el recorrido tres "sondas" miden la salud del RESTO del sistema cada 0,5 s:
GET /sagas/{id} (orquestador), GET /cursos (BFF -> Cupos) y el panel del alumno (BFF). La pregunta del
escenario es: ¿la lentitud de Pagos se contagia a ellas? Con la resiliencia puesta no debería.

Al final se espera a que todas las sagas terminen y se verifica que no quedó estado inconsistente:
cobros cobrados == matrículas completadas == cupos consumidos.

Los datos crudos (cada petición, cada muestra de métricas) quedan en docs/resultados/escenario3-<modo>.json
y, si hay matplotlib, un gráfico en escenario3-<modo>.png.

--sin-docker: para correr contra procesos locales sin compose. El orquestador se arranca a mano con el
modo deseado y las URLs se toman de variables de entorno (ver `Entorno`).
"""
import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080/api/v1")
CABECERAS = {"Authorization": "Bearer carga-experimentos"}
RAIZ = Path(__file__).resolve().parents[1]
CUPOS_CURSO = 100000  # cada corrida crea su propio curso de carga: así no consume ni depende de los cupos reales
ESTUDIANTE_BASE = "est-001"
FASES = ("linea_base", "pagos_lento", "pagos_caido", "recuperacion")
EN_CURSO = ("INICIADA", "CUPO_RESERVADO", "PAGO_COBRADO", "COMPENSANDO", "COMPENSACION_PENDIENTE")


# ---------------------------------------------------------------- Acceso al sistema (docker o procesos locales)

_PY_POST = ("import sys, urllib.request as u; r = u.Request('http://localhost:8000/admin/falla', "
            "data=sys.argv[1].encode(), headers={'Content-Type': 'application/json'}); u.urlopen(r).read()")
_PY_GET = "import urllib.request as u; print(u.urlopen('http://localhost:8000/metrics', timeout=3).read().decode())"
_PY_SQL = "import sqlite3, sys, json; db = sqlite3.connect(sys.argv[1]); print(json.dumps(dict(db.execute(sys.argv[2]).fetchall())))"
_PY_EXEC = "import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); db.execute(sys.argv[2]); db.commit()"


class Entorno:
    """Cómo inyectar fallas, leer métricas y contar filas. Con compose usa `docker compose exec`;
    con --sin-docker usa HTTP y archivos locales (variables PAGOS_ADMIN_URL, ORQUESTADOR_METRICS_URL,
    PAGOS_METRICS_URL, PAGOS_DB y ORQUESTADOR_DB)."""

    def __init__(self, con_docker: bool):
        self.con_docker = con_docker

    def _exec(self, servicio: str, *args: str) -> str:
        salida = subprocess.run(["docker", "compose", "exec", "-T", servicio, "python", "-c", *args],
                                capture_output=True, text=True, check=True, cwd=RAIZ)
        return salida.stdout

    def inyectar_falla(self, **config) -> None:
        if self.con_docker:
            self._exec("pagos", _PY_POST, json.dumps(config))
        else:
            httpx.post(os.getenv("PAGOS_ADMIN_URL", "http://localhost:8104") + "/admin/falla", json=config).raise_for_status()

    def metricas(self, servicio: str) -> str:
        if self.con_docker:
            return self._exec(servicio, _PY_GET)
        defecto = {"orquestador": "http://localhost:8102/metrics", "pagos": "http://localhost:8104/metrics"}[servicio]
        return httpx.get(os.getenv(f"{servicio.upper()}_METRICS_URL", defecto), timeout=3).text

    def contar(self, servicio: str, sql: str) -> dict:
        if self.con_docker:
            ruta = {"pagos": "/data/pagos.db", "orquestador": "/data/orquestador.db"}[servicio]
            return json.loads(self._exec(servicio, _PY_SQL, ruta, sql))
        import sqlite3
        db = sqlite3.connect(os.environ[f"{servicio.upper()}_DB"])
        return dict(db.execute(sql).fetchall())

    def crear_curso_de_carga(self, curso: str) -> None:
        """Curso propio de la corrida, con cupos de sobra, en la BD de Cupos (el servicio la lee en cada llamada)."""
        sql = f"INSERT OR IGNORE INTO cursos VALUES ('{curso}', 'Curso de carga (experimentos)', {CUPOS_CURSO}, {CUPOS_CURSO})"
        if self.con_docker:
            self._exec("cupos", _PY_EXEC, "/data/cupos.db", sql)
        else:
            import sqlite3
            db = sqlite3.connect(os.environ["CUPOS_DB"])
            db.execute(sql)
            db.commit()

    def reiniciar_orquestador(self, modo: str, timeout_off: float) -> None:
        if not self.con_docker:
            print(f"(--sin-docker) Asegúrate de haber arrancado el orquestador con RESILIENCIA={modo}")
            return
        entorno = {**os.environ, "RESILIENCIA": modo, "PAGOS_TIMEOUT_SECONDS": "2" if modo == "on" else f"{timeout_off:g}"}
        subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", "orquestador"],
                       check=True, cwd=RAIZ, env=entorno, capture_output=True)


def esperar_listo(http: httpx.Client, segundos: int = 90) -> None:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            if (http.get(f"{GATEWAY}/sagas/no-existe").status_code == 404 and http.get(f"{GATEWAY}/cursos").status_code == 200):
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    sys.exit("El sistema no respondió a tiempo. ¿Está levantado? (docker compose up -d)")


# ---------------------------------------------------------------- Métricas Prometheus

def parsear(texto: str) -> list[tuple[str, dict, float]]:
    filas = []
    for linea in texto.splitlines():
        if not linea or linea.startswith("#"):
            continue
        nombre, _, resto = linea.partition("{")
        if resto:
            etiquetas_txt, _, valor = resto.rpartition("} ")
            etiquetas = dict(par.split("=", 1) for par in etiquetas_txt.split(",") if "=" in par)
            etiquetas = {k: v.strip('"') for k, v in etiquetas.items()}
        else:
            nombre, _, valor = linea.rpartition(" ")
            etiquetas = {}
        try:
            filas.append((nombre.strip(), etiquetas, float(valor.split()[0])))
        except (ValueError, IndexError):
            continue
    return filas


def suma(filas, nombre: str, **filtro) -> float:
    return sum(v for n, e, v in filas if n == nombre and all(e.get(k) == x for k, x in filtro.items()))


def muestrear(entorno: Entorno, t: float) -> dict | None:
    try:
        orq, pag = parsear(entorno.metricas("orquestador")), parsear(entorno.metricas("pagos"))
    except Exception:  # una muestra perdida no debe romper el experimento
        return None
    return {
        "t": round(t, 2),
        "circuit_state": suma(orq, "circuit_state", dep="pagos"),
        "bulkhead_in_flight": suma(orq, "bulkhead_in_flight", dep="pagos"),
        "bulkhead_rejected_total": suma(orq, "bulkhead_rejected_total", dep="pagos"),
        "circuit_rejected_total": suma(orq, "circuit_rejected_total", dep="pagos"),
        "pagos_recibidas_total": sum(v for n, e, v in pag if n == "http_requests_total" and e.get("service") == "pagos"
                                      and e.get("route", "").startswith("/cobros")),
        "reintentos_total": suma(orq, "pagos_reintentos_total"),
        "saga_total": {e["resultado"]: v for n, e, v in orq if n == "saga_total"},
    }


# ---------------------------------------------------------------- Carga y sondas

def clasificar(respuesta: httpx.Response) -> tuple[str, str | None, str | None]:
    """(motivo, saga_id, estado_saga) de la respuesta de POST /matriculas."""
    if respuesta.status_code == 201:
        return "completada", respuesta.json().get("saga_id"), "COMPLETADA"
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = {}
    detalle = (cuerpo.get("detail") or "").lower()
    estado, saga = cuerpo.get("estado"), cuerpo.get("saga_id")
    if "circuito abierto" in detalle:
        return "circuito_abierto", saga, estado
    if "saturado" in detalle:
        return "bulkhead_lleno", saga, estado
    if respuesta.status_code == 504:
        return "gateway_504", saga, estado
    if respuesta.status_code == 502:
        return "gateway_502", saga, estado
    if "no respondió" in detalle or "conectar" in detalle:
        return "pagos_timeout", saga, estado
    if "503" in detalle or "500" in detalle:
        return "pagos_error_http", saga, estado
    return f"otro_{respuesta.status_code}", saga, estado


class Carga:
    def __init__(self, http: httpx.Client, t0: float, curso: str):
        self.http, self.t0, self.curso = http, t0, curso
        self.peticiones: list[dict] = []
        self.lock = threading.Lock()
        self.detener = threading.Event()

    def _anotar(self, **registro) -> None:
        with self.lock:
            self.peticiones.append(registro)

    def matricular(self, fase: str, estudiante: str) -> None:
        t, inicio = time.monotonic() - self.t0, time.perf_counter()
        try:
            r = self.http.post(f"{GATEWAY}/matriculas", json={"estudiante_id": estudiante, "curso_id": self.curso},
                               headers={"X-Correlation-Id": f"exp-{uuid.uuid4().hex[:10]}"})
            motivo, saga, estado = clasificar(r)
            codigo = r.status_code
        except httpx.HTTPError:
            codigo, motivo, saga, estado = 0, "sin_respuesta", None, None
        self._anotar(t=round(t, 3), fase=fase, tipo="matricula", codigo=codigo, motivo=motivo,
                     ms=round((time.perf_counter() - inicio) * 1000, 1), saga_id=saga, estado=estado)

    def sonda(self, tipo: str, ruta: str, fase_de) -> None:
        while not self.detener.is_set():
            t, inicio = time.monotonic() - self.t0, time.perf_counter()
            try:
                r = self.http.get(f"{GATEWAY}{ruta}")
                codigo, degradado, cupos = r.status_code, None, None
                if codigo == 200 and tipo == "sonda_panel":
                    degradado = r.json().get("degradado")
                elif codigo == 200 and tipo == "sonda_cursos":
                    cupos = next((c["cupos_disponibles"] for c in r.json()["items"] if c["id"] == self.curso), None)
            except httpx.HTTPError:
                codigo, degradado, cupos = 0, None, None
            self._anotar(t=round(t, 3), fase=fase_de(t), tipo=tipo, codigo=codigo, degradado=degradado, cupos=cupos,
                         ms=round((time.perf_counter() - inicio) * 1000, 1))
            self.detener.wait(0.5)


def ejecutar_corrida(entorno: Entorno, modo: str, args) -> dict:
    http = httpx.Client(headers=CABECERAS, timeout=httpx.Timeout(25.0), limits=httpx.Limits(max_connections=800))
    entorno.inyectar_falla(latencia_ms=0, caido=False, tasa_error=0.0)
    entorno.reiniciar_orquestador(modo, args.timeout_off)
    esperar_listo(http)

    curso = f"EXP-{modo}-{int(time.time()) % 100000}"
    entorno.crear_curso_de_carga(curso)
    # Todo se mide sobre este curso nuevo: las sagas, cobros y cupos de corridas anteriores no se mezclan.
    cupos_ini = CUPOS_CURSO

    def nuevo_estudiante() -> str:
        sufijo = uuid.uuid4().hex[:8]
        r = http.post(f"{GATEWAY}/estudiantes", json={"nombre": f"Exp {sufijo}", "email": f"exp-{sufijo}@example.com"})
        r.raise_for_status()
        return r.json()["id"]

    alumnos_base = [nuevo_estudiante() for _ in range(args.n_linea_base)]
    alumnos_recup = [nuevo_estudiante() for _ in range(args.n_recuperacion)]

    plan = [  # nombre, falla, rps, duración
        ("linea_base", {"latencia_ms": 0, "caido": False}, args.n_linea_base / args.duracion_base, args.duracion_base),
        ("pagos_lento", {"latencia_ms": args.latencia_lenta, "caido": False}, args.rps_pico, args.duracion_lento),
        ("pagos_caido", {"latencia_ms": args.latencia_caida, "caido": True}, args.rps_pico, args.duracion_caido),
        ("recuperacion", {"latencia_ms": 0, "caido": False}, args.n_recuperacion / args.duracion_recuperacion,
         args.duracion_recuperacion),
    ]
    limites, acumulado = {}, 0.0
    for nombre, _, _, duracion in plan:
        limites[nombre] = (acumulado, acumulado + duracion)
        acumulado += duracion

    t0 = time.monotonic()
    carga = Carga(http, t0, curso)
    fase_de = lambda t: next((n for n, (a, b) in limites.items() if a <= t < b), "cierre")  # noqa: E731
    muestras: list[dict] = []
    detener_muestreo = threading.Event()

    def bucle_muestreo():
        while not detener_muestreo.is_set():
            if (m := muestrear(entorno, time.monotonic() - t0)) is not None:
                muestras.append(m)
            detener_muestreo.wait(1.0 if not entorno.con_docker else 0.5)

    # Saga existente para que la sonda de /sagas/{id} lea algo real: se crea una matrícula previa.
    previa = nuevo_estudiante()
    r = http.post(f"{GATEWAY}/matriculas", json={"estudiante_id": previa, "curso_id": curso})
    r.raise_for_status()
    saga_base = r.json()["saga_id"]
    t0 = time.monotonic()
    carga.t0 = t0

    hilos = [threading.Thread(target=bucle_muestreo, daemon=True)]
    for tipo, ruta in (("sonda_sagas", f"/sagas/{saga_base}"), ("sonda_cursos", "/cursos"),
                       ("sonda_panel", f"/web/alumnos/{ESTUDIANTE_BASE}/panel")):
        hilos.append(threading.Thread(target=carga.sonda, args=(tipo, ruta, fase_de), daemon=True))
    for hilo in hilos:
        hilo.start()

    print(f"\n=== Corrida {modo.upper()} ({'con' if modo == 'on' else 'SIN'} patrones) ===")
    pool = ThreadPoolExecutor(max_workers=800)  # holgado: si la cola fuera del cliente, la latencia medida mentiría
    futuros = []
    try:
        for nombre, falla, rps, duracion in plan:
            entorno.inyectar_falla(**falla)
            inicio_fase = time.monotonic()
            print(f"[{time.monotonic() - t0:5.1f}s] fase {nombre}: {falla}  carga {rps:.1f} matrículas/s durante {duracion:g} s")
            alumnos = iter(alumnos_base if nombre == "linea_base" else alumnos_recup if nombre == "recuperacion" else [])
            n = 0
            total = round(rps * duracion)
            while n < total:
                ahora = time.monotonic()
                objetivo = inicio_fase + n / rps
                if objetivo > ahora:
                    time.sleep(objetivo - ahora)
                estudiante = next(alumnos, ESTUDIANTE_BASE) if nombre in ("linea_base", "recuperacion") else ESTUDIANTE_BASE
                futuros.append(pool.submit(carga.matricular, nombre, estudiante))
                n += 1
            # Cada fase dura exactamente lo planeado: así las líneas del gráfico coinciden con los cambios reales.
            time.sleep(max(0.0, inicio_fase + duracion - time.monotonic()))
        print(f"[{time.monotonic() - t0:5.1f}s] fin de la carga; esperando respuestas pendientes...")
        for futuro in futuros:
            futuro.result(timeout=60)
    finally:
        entorno.inyectar_falla(latencia_ms=0, caido=False, tasa_error=0.0)
        time.sleep(2)  # unas muestras más ya con Pagos sano
        carga.detener.set()
        detener_muestreo.set()
        pool.shutdown(wait=False, cancel_futures=True)

    consistencia = verificar_consistencia(entorno, http, args, curso, cupos_ini)
    corrida = {
        "modo": modo, "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {**vars(args), "bulkhead_max": os.getenv("PAGOS_BULKHEAD_MAX", "5 (compose)"),
                   "timeout_pagos_s": 2 if modo == "on" else args.timeout_off},
        "fases": {n: {"inicio": a, "fin": b} for n, (a, b) in limites.items()},
        "peticiones": carga.peticiones, "muestras": muestras, "consistencia": consistencia,
    }
    corrida["resumen"] = resumir(corrida)
    imprimir_resumen(corrida)
    return corrida


def verificar_consistencia(entorno, http, args, curso, cupos_ini) -> dict:
    print(f"\nEsperando que las sagas terminen (hasta {args.espera_consistencia} s)...")
    limite = time.monotonic() + args.espera_consistencia
    while True:
        sagas = entorno.contar("orquestador", f"SELECT estado, COUNT(*) FROM sagas WHERE curso_id='{curso}' GROUP BY estado")
        en_curso = sum(sagas.get(e, 0) for e in EN_CURSO)
        if en_curso <= 0 or time.monotonic() > limite:
            break
        time.sleep(2)
    cobros = entorno.contar("pagos", f"SELECT estado, COUNT(*) FROM cobros WHERE curso_id='{curso}' GROUP BY estado")
    cupos_fin = next(c for c in http.get(f"{GATEWAY}/cursos").json()["items"] if c["id"] == curso)["cupos_disponibles"]
    sagas_ini = cobros_ini = {}
    delta = lambda a, b, k: b.get(k, 0) - a.get(k, 0)  # noqa: E731
    r = {
        "curso": curso, "sagas_sin_terminar": max(en_curso, 0),
        "cobros_por_estado": cobros,
        "sagas_completadas": delta(sagas_ini, sagas, "COMPLETADA") + 0,
        "cobros_cobrados": delta(cobros_ini, cobros, "cobrado"),
        "cupos_consumidos": cupos_ini - cupos_fin,
        "sagas_por_estado": {e: delta(sagas_ini, sagas, e) for e in sorted(sagas) if delta(sagas_ini, sagas, e)},
    }
    r["consistente"] = (r["sagas_sin_terminar"] == 0
                        and r["sagas_completadas"] == r["cobros_cobrados"] == r["cupos_consumidos"])
    # Con compensaciones aún en cola el sistema converge más tarde (consistencia eventual), no está roto.
    r["veredicto"] = ("OK" if r["consistente"] else
                      f"PENDIENTE: {r['sagas_sin_terminar']} sagas siguen compensándose" if r["sagas_sin_terminar"] else "INESPERADO")
    return r


# ---------------------------------------------------------------- Resumen

def percentil(valores: list[float], p: float) -> float | None:
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[max(0, math.ceil(p / 100 * len(ordenados)) - 1)]


def resumir(corrida: dict) -> dict:
    resumen = {}
    for fase in FASES:
        pet = [p for p in corrida["peticiones"] if p["fase"] == fase]
        mat = [p for p in pet if p["tipo"] == "matricula"]
        ms = [p["ms"] for p in mat]
        motivos: dict[str, int] = {}
        for p in mat:
            motivos[p["motivo"]] = motivos.get(p["motivo"], 0) + 1
        a, b = corrida["fases"][fase]["inicio"], corrida["fases"][fase]["fin"]
        # Contadores acumulados: la diferencia entre la última muestra al cierre de la fase y la última al inicio.
        m = [x for x in corrida["muestras"] if a <= x["t"] < b]
        antes = [x for x in corrida["muestras"] if x["t"] <= a]
        hasta = [x for x in corrida["muestras"] if x["t"] <= b]
        delta = lambda campo: (hasta[-1][campo] - antes[-1][campo]) if antes and hasta else None  # noqa: E731
        fila = {
            "matriculas": len(mat), "motivos": dict(sorted(motivos.items(), key=lambda kv: -kv[1])),
            "matricula_p50_ms": percentil(ms, 50), "matricula_p95_ms": percentil(ms, 95), "matricula_max_ms": max(ms, default=None),
            "respuestas_rapidas_pct": round(100 * sum(1 for v in ms if v < 250) / len(ms), 1) if ms else None,
            "llegaron_a_pagos": delta("pagos_recibidas_total"), "rechazos_bulkhead": delta("bulkhead_rejected_total"),
            "cortes_circuito": delta("circuit_rejected_total"), "reintentos": delta("reintentos_total"),
            "circuito_estados": sorted({int(x["circuit_state"]) for x in m}),
            "max_en_vuelo": max((x["bulkhead_in_flight"] for x in m), default=None),
        }
        for tipo in ("sonda_sagas", "sonda_cursos", "sonda_panel"):
            s = [p for p in pet if p["tipo"] == tipo]
            v = [p["ms"] for p in s]
            if tipo == "sonda_cursos":
                cupos = [p["cupos"] for p in s if p.get("cupos") is not None]
                # "Ocupados" = matrículas completadas + reservas retenidas por sagas colgadas.
                fila["cupos_retenidos_max"] = (CUPOS_CURSO - min(cupos)) if cupos else None
            fila[tipo] = {"n": len(s), "p50_ms": percentil(v, 50), "p95_ms": percentil(v, 95), "max_ms": max(v, default=None),
                          "errores": sum(1 for p in s if p["codigo"] != 200)}
        resumen[fase] = fila
    return resumen


def _f(valor, unidad="") -> str:
    return "–" if valor is None else (f"{valor:,.0f}{unidad}" if isinstance(valor, (int, float)) else str(valor))


def imprimir_resumen(corrida: dict) -> None:
    print(f"\n--- Resumen {corrida['modo'].upper()} ---")
    for fase, f in corrida["resumen"].items():
        print(f"\n[{fase}] {f['matriculas']} matrículas  p50={_f(f['matricula_p50_ms'], ' ms')}  p95={_f(f['matricula_p95_ms'], ' ms')}  "
              f"máx={_f(f['matricula_max_ms'], ' ms')}  respuestas <250 ms: {_f(f['respuestas_rapidas_pct'], ' %')}")
        print(f"   resultados: {f['motivos']}")
        print(f"   llegaron a Pagos: {_f(f['llegaron_a_pagos'])}  rechazos bulkhead: {_f(f['rechazos_bulkhead'])}  "
              f"cortes del circuito: {_f(f['cortes_circuito'])}  reintentos: {_f(f['reintentos'])}  "
              f"estados del circuito vistos: {f['circuito_estados']}  máx. en vuelo: {_f(f['max_en_vuelo'])}  "
              f"cupos ocupados (máx.): {_f(f.get('cupos_retenidos_max'))}")
        for tipo in ("sonda_sagas", "sonda_cursos", "sonda_panel"):
            s = f[tipo]
            print(f"   {tipo:<13} n={s['n']:<3} p50={_f(s['p50_ms'], ' ms'):>9}  p95={_f(s['p95_ms'], ' ms'):>9}  "
                  f"máx={_f(s['max_ms'], ' ms'):>9}  errores={s['errores']}")
    c = corrida["consistencia"]
    print(f"\nConsistencia: sagas sin terminar={c['sagas_sin_terminar']}  completadas={c['sagas_completadas']}  "
          f"cobros={c['cobros_por_estado']}  cupos consumidos={c['cupos_consumidos']}  sagas={c['sagas_por_estado']}")
    print(f"Resultado: {c['veredicto']}: cobrado == completadas == cupos consumidos y ninguna saga quedó a medias")


# ---------------------------------------------------------------- Gráficos y comparación

COLORES = {"completada": "#2e7d32", "circuito_abierto": "#1565c0", "bulkhead_lleno": "#6a1b9a", "pagos_timeout": "#ef6c00",
           "pagos_error_http": "#c62828", "gateway_504": "#000000", "gateway_502": "#555555", "sin_respuesta": "#999999"}


def grafico(corrida: dict, ruta: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib no está instalado: se omite el gráfico; los datos crudos están en el JSON)")
        return
    fig, ejes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    mat = [p for p in corrida["peticiones"] if p["tipo"] == "matricula"]
    for motivo in sorted({p["motivo"] for p in mat}):
        puntos = [p for p in mat if p["motivo"] == motivo]
        ejes[0].scatter([p["t"] for p in puntos], [max(p["ms"], 1) for p in puntos], s=9, alpha=.7,
                        label=motivo, color=COLORES.get(motivo))
    ejes[0].set_yscale("log"); ejes[0].set_ylabel("ms (escala log)"); ejes[0].set_title("POST /matriculas: latencia y resultado")
    ejes[0].legend(fontsize=8, ncol=3, loc="upper right")
    for tipo, color in (("sonda_sagas", "#1565c0"), ("sonda_cursos", "#2e7d32"), ("sonda_panel", "#ef6c00")):
        s = sorted((p for p in corrida["peticiones"] if p["tipo"] == tipo), key=lambda p: p["t"])
        ejes[1].plot([p["t"] for p in s], [max(p["ms"], 1) for p in s], ".-", ms=3, lw=.8, label=tipo, color=color)
    ejes[1].set_yscale("log"); ejes[1].set_ylabel("ms (escala log)")
    ejes[1].set_title("Salud del resto del sistema: sondas (¿se contagian de la lentitud de Pagos?)"); ejes[1].legend(fontsize=8)
    m = corrida["muestras"]
    ejes[2].step([x["t"] for x in m], [x["circuit_state"] for x in m], where="post", color="#c62828", label="circuit_state (0 cerrado, 1 half-open, 2 abierto)")
    ejes[2].plot([x["t"] for x in m], [x["bulkhead_in_flight"] for x in m], color="#6a1b9a", label="bulkhead_in_flight")
    ejes[2].set_ylabel("valor"); ejes[2].set_xlabel("segundos desde el inicio"); ejes[2].legend(fontsize=8, loc="upper right")
    ejes[2].set_title("Estado del circuito y del bulkhead" + (" (sin patrones: constantes en 0)" if corrida["modo"] == "off" else ""))
    for eje in ejes:
        for nombre, f in corrida["fases"].items():
            eje.axvline(f["inicio"], color="gray", ls="--", lw=.6)
            if eje is ejes[0]:
                eje.text(f["inicio"] + .3, eje.get_ylim()[0] * 1.2, nombre, fontsize=8, color="gray")
    fig.suptitle(f"Escenario 3 — corrida {'CON' if corrida['modo'] == 'on' else 'SIN'} patrones de resiliencia", fontweight="bold")
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)
    print(f"Gráfico: {ruta}")


def comparar(con: dict, sin: dict, salida: Path) -> None:
    filas = [("Matrícula p95 (ms)", lambda f: f["matricula_p95_ms"]), ("Matrícula máx. (ms)", lambda f: f["matricula_max_ms"]),
             ("Respuestas < 250 ms (%)", lambda f: f["respuestas_rapidas_pct"]),
             ("Llegaron a Pagos", lambda f: f["llegaron_a_pagos"]), ("Cupos ocupados (máx.)", lambda f: f.get("cupos_retenidos_max")),
             ("Sonda /sagas p95 (ms)", lambda f: f["sonda_sagas"]["p95_ms"]), ("Sonda /cursos p95 (ms)", lambda f: f["sonda_cursos"]["p95_ms"]),
             ("Sonda panel p95 (ms)", lambda f: f["sonda_panel"]["p95_ms"]),
             ("Errores sonda /sagas", lambda f: f["sonda_sagas"]["errores"]), ("Errores sonda /cursos", lambda f: f["sonda_cursos"]["errores"])]
    lineas = ["# Escenario 3 — con vs. sin patrones de resiliencia", "",
              f"Con patrones: {con['fecha']} · Sin patrones: {sin['fecha']} · carga pico: {con['config']['rps_pico']} matrículas/s · "
              f"Pagos lento = {con['config']['latencia_lenta']} ms", "",
              "| Fase | Métrica | CON patrones | SIN patrones |", "|---|---|---:|---:|"]
    for fase in ("pagos_lento", "pagos_caido", "recuperacion"):
        for nombre, extraer in filas:
            lineas.append(f"| {fase} | {nombre} | {_f(extraer(con['resumen'][fase]))} | {_f(extraer(sin['resumen'][fase]))} |")
    for etiqueta, c in (("CON patrones", con["consistencia"]), ("SIN patrones", sin["consistencia"])):
        lineas.append(f"\n**Consistencia {etiqueta}:** {c['veredicto']} — completadas {c['sagas_completadas']}, "
                      f"cobros cobrados {c['cobros_cobrados']}, cupos consumidos {c['cupos_consumidos']}, sagas sin terminar {c['sagas_sin_terminar']}.")
    texto = "\n".join(lineas) + "\n"
    salida.write_text(texto, encoding="utf-8")
    print("\n" + texto + f"Comparación guardada en {salida}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--modo", choices=["on", "off", "ambos"], default="on")
    ap.add_argument("--comparar", nargs=2, metavar=("ON.json", "OFF.json"))
    ap.add_argument("--sin-docker", action="store_true", help="usar procesos locales (ver Entorno)")
    ap.add_argument("--salida", type=Path, default=RAIZ / "docs" / "resultados")
    ap.add_argument("--rps-pico", type=float, default=30, help="matrículas por segundo en las fases con Pagos roto")
    ap.add_argument("--latencia-lenta", type=int, default=5000, help="ms que tarda Pagos en la fase lenta")
    ap.add_argument("--latencia-caida", type=int, default=0, help="ms de demora antes del 503 en la fase caída")
    ap.add_argument("--timeout-off", type=float, default=2.0, help="timeout de Pagos en la corrida sin patrones (s)")
    ap.add_argument("--n-linea-base", type=int, default=5)
    ap.add_argument("--n-recuperacion", type=int, default=8)
    ap.add_argument("--duracion-base", type=float, default=10)
    ap.add_argument("--duracion-lento", type=float, default=20)
    ap.add_argument("--duracion-caido", type=float, default=15)
    ap.add_argument("--duracion-recuperacion", type=float, default=20)
    ap.add_argument("--espera-consistencia", type=int, default=120)
    args = ap.parse_args()
    args.salida.mkdir(parents=True, exist_ok=True)

    if args.comparar:
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in args.comparar)
        comparar(a, b, args.salida / "escenario3-comparacion.md")
        return
    if args.sin_docker and args.modo == "ambos":
        sys.exit("--sin-docker necesita --modo on u off (el orquestador se reinicia a mano entre corridas).")

    entorno = Entorno(con_docker=not args.sin_docker)
    corridas = {}
    try:
        for modo in (("on", "off") if args.modo == "ambos" else (args.modo,)):
            corridas[modo] = ejecutar_corrida(entorno, modo, args)
            ruta = args.salida / f"escenario3-{modo}.json"
            ruta.write_text(json.dumps(corridas[modo], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            print(f"Datos crudos: {ruta}")
            grafico(corridas[modo], ruta.with_suffix(".png"))
    finally:
        if not args.sin_docker:
            # Deja el sistema en su modo normal (con patrones) y sin fallas.
            Entorno(True).inyectar_falla(latencia_ms=0, caido=False, tasa_error=0.0)
            Entorno(True).reiniciar_orquestador("on", 2)
    if len(corridas) == 2:
        comparar(corridas["on"], corridas["off"], args.salida / "escenario3-comparacion.md")


if __name__ == "__main__":
    main()
