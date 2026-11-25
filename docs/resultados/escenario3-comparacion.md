# Escenario 3 — con vs. sin patrones de resiliencia

Con patrones: 2026-11-24 20:08:54 · Sin patrones: 2026-11-24 20:10:09 · carga pico: 30 matrículas/s · Pagos lento = 5000 ms

| Fase | Métrica | CON patrones | SIN patrones |
|---|---|---:|---:|
| pagos_lento | Matrícula p95 (ms) | 70 | 15,711 |
| pagos_lento | Matrícula máx. (ms) | 2,249 | 17,847 |
| pagos_lento | Respuestas < 250 ms (%) | 99 | 0 |
| pagos_lento | Llegaron a Pagos | 15 | 2 |
| pagos_lento | Cupos ocupados (máx.) | 15 | 225 |
| pagos_lento | Sonda /sagas p95 (ms) | 10 | 9,689 |
| pagos_lento | Sonda /cursos p95 (ms) | 12 | 5,022 |
| pagos_lento | Sonda panel p95 (ms) | 2,014 | 5,512 |
| pagos_lento | Errores sonda /sagas | 0 | 2 |
| pagos_lento | Errores sonda /cursos | 0 | 1 |
| pagos_caido | Matrícula p95 (ms) | 66 | 8,062 |
| pagos_caido | Matrícula máx. (ms) | 73 | 9,016 |
| pagos_caido | Respuestas < 250 ms (%) | 100 | 31 |
| pagos_caido | Llegaron a Pagos | 0 | 327 |
| pagos_caido | Cupos ocupados (máx.) | 15 | 224 |
| pagos_caido | Sonda /sagas p95 (ms) | 13 | 908 |
| pagos_caido | Sonda /cursos p95 (ms) | 10 | 2,393 |
| pagos_caido | Sonda panel p95 (ms) | 13 | 5,866 |
| pagos_caido | Errores sonda /sagas | 0 | 0 |
| pagos_caido | Errores sonda /cursos | 0 | 0 |
| recuperacion | Matrícula p95 (ms) | 91 | 105 |
| recuperacion | Matrícula máx. (ms) | 91 | 105 |
| recuperacion | Respuestas < 250 ms (%) | 100 | 100 |
| recuperacion | Llegaron a Pagos | 46 | 248 |
| recuperacion | Cupos ocupados (máx.) | 18 | 211 |
| recuperacion | Sonda /sagas p95 (ms) | 8 | 10 |
| recuperacion | Sonda /cursos p95 (ms) | 10 | 24 |
| recuperacion | Sonda panel p95 (ms) | 12 | 25 |
| recuperacion | Errores sonda /sagas | 0 | 0 |
| recuperacion | Errores sonda /cursos | 0 | 0 |

**Consistencia CON patrones:** OK — completadas 13, cobros cobrados 13, cupos consumidos 13, sagas sin terminar 0.

**Consistencia SIN patrones:** OK — completadas 14, cobros cobrados 14, cupos consumidos 14, sagas sin terminar 0.
