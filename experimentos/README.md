# Experimentos — Escenario 3 (falla en cascada contenida)

`experimento.py` provoca la falla, mide el sistema y deja los datos crudos. No hace falta instalar
nada aparte de lo de `requirements-dev.txt` (matplotlib es opcional: sin él solo se omite el gráfico).

```bash
docker compose up --build -d                       # sistema normal, con patrones
python experimentos/experimento.py --modo on       # con timeout + reintento + circuit breaker + bulkhead
python experimentos/experimento.py --modo off      # "sin patrón": RESILIENCIA=off (queda solo el timeout de 2 s)
python experimentos/experimento.py --modo ambos    # las dos corridas y la comparación (~6 min)
```

Cada corrida reinicia el orquestador con el modo pedido (`docker compose up -d --force-recreate orquestador`,
usando las variables `RESILIENCIA` y `PAGOS_TIMEOUT_SECONDS` que lee el compose) y al terminar lo deja de
nuevo en modo normal y sin fallas en Pagos.

## Qué hace (≈ 65 s de carga)

| Fase | Duración | Falla inyectada en Pagos (`/admin/falla`) | Carga por el gateway |
|---|---|---|---|
| `linea_base` | 10 s | ninguna | 5 matrículas reales |
| `pagos_lento` | 20 s | `latencia_ms=5000` | **pico: 30 matrículas/s** (carga abierta, no espera respuestas) |
| `pagos_caido` | 15 s | `caido=true` (responde 503) | 30 matrículas/s |
| `recuperacion` | 20 s | ninguna | 8 matrículas reales |

Mientras tanto, tres **sondas** miden cada 0,5 s la salud del *resto* del sistema (todas por el gateway):
`GET /sagas/{id}` (orquestador), `GET /cursos` (BFF → Cupos) y `GET /web/alumnos/est-001/panel` (BFF). La
pregunta es si la lentitud de Pagos se contagia a ellas. Además se muestrea cada ~1 s el `/metrics` del
orquestador y de Pagos (`circuit_state`, `bulkhead_in_flight`, `bulkhead_rejected_total`, `circuit_rejected_total`,
llamadas que *llegaron* a Pagos, reintentos).

Cada corrida crea su propio curso `EXP-<modo>-<n>` con 100 000 cupos, así que **no consume los cupos
reales** ni se mezcla con otras corridas. Usa el token `carga-experimentos` (límite de 100 000 por
ventana) para que el rate limit del gateway no contamine la medición.

## Qué queda como evidencia (`docs/resultados/`)

- `escenario3-on.json` / `escenario3-off.json`: **datos crudos** (cada petición con su instante, código,
  latencia y resultado; cada muestra de métricas; el resumen por fase; la verificación de consistencia).
- `escenario3-on.png` / `escenario3-off.png`: gráfico de tres paneles (latencia y resultado de cada matrícula,
  sondas, estado del circuito y del bulkhead), con las fases marcadas.
- `escenario3-comparacion.md`: tabla con vs. sin patrones (la genera `--modo ambos` o
  `--comparar docs/resultados/escenario3-on.json docs/resultados/escenario3-off.json`).

Para el informe, tomar además las **capturas de Grafana** (http://localhost:3000, rango de las últimas 5 min)
mientras corre cada fase: *Estado del circuito*, *Bulkhead: en vuelo y rechazos*, *Latencia p95 por servicio* y
*Sagas por resultado*.

## Consistencia al final

Tras la carga se espera (hasta 120 s) a que no queden sagas a medias y se comprueba, sobre el curso de la
corrida: `cobros cobrados == sagas COMPLETADAS == cupos consumidos`. Si quedan compensaciones en cola el
veredicto dice `PENDIENTE` (el sistema converge más tarde; no es un error), no `OK`.

## Parámetros útiles

`--rps-pico 30` · `--latencia-lenta 5000` · `--latencia-caida 0` (ms antes del 503; con 3000 simula una pasarela
que se cuelga y luego falla) · `--timeout-off 2` (con `--timeout-off 10` la corrida sin patrón también tiene un
timeout holgado, el cliente ingenuo) · `--duracion-lento/--duracion-caido/...` · `--sin-docker` (procesos locales).

## Cómo se prueba cada patrón por separado

- **Solo timeout**: `RESILIENCIA=off` (corrida `off`).
- **Bulkhead**: en la fase lenta, `bulkhead_rejected_total` sube y `bulkhead_in_flight` no pasa de 5
  (`PAGOS_BULKHEAD_MAX`).
- **Circuit breaker**: `circuit_state` pasa 0 → 2 (abre) → 1 (half-open) → 0 (cierra) y
  `circuit_rejected_total` cuenta las llamadas cortadas sin llamar a Pagos.
- Los tests unitarios de estas piezas: `python -m unittest tests.test_resiliencia -v`.
