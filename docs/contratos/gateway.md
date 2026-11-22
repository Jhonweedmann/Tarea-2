# Gateway — API pública (dueño: A)

Base: `http://localhost:8080`. Única puerta de entrada al sistema.

## Autenticación

`Authorization: Bearer <token>`. Cada token identifica a un **cliente** con un rol y un límite de
solicitudes. Los tokens de desarrollo (configurables con la variable `GATEWAY_CLIENTES`, JSON):

| Token | Cliente (`X-Client-Id`) | Rol | Límite |
|---|---|---|---|
| `desarrollo-seguro` | `secretaria` | escritura | 20 / 10 s |
| `solo-lectura` | `consulta` | lectura (solo `GET`, `HEAD`, `OPTIONS`) | 20 / 10 s |
| `portal-web` | `portal-web` | escritura | 20 / 10 s |
| `carga-experimentos` | `experimentos` | escritura | 100 000 / 10 s (para las pruebas de carga de C) |

| Caso | Respuesta |
|---|---|
| Sin cabecera o token desconocido | `401` + `WWW-Authenticate: Bearer` (`error="invalid_token"` si el token es desconocido) |
| Token de lectura en `POST` | `403` |
| Supera su límite en la ventana | `429` + `Retry-After: <segundos>` |
| Ruta que no existe | `404` |
| El servicio interno no responde a tiempo | `504` |
| El servicio interno no acepta conexiones | `502` |

Todas en `application/problem+json`. Toda respuesta trae `X-Correlation-Id`, y las autenticadas
traen `X-RateLimit-Limit` y `X-RateLimit-Remaining`.

**Rate limit:** ventana fija por cliente en Redis (`INCR` + `EXPIRE` sobre
`ratelimit:<cliente>:<n° de ventana>`). Si Redis no responde, el gateway **deja pasar** la
solicitud (fail-open) y deja de intentar Redis por 15 s: el límite protege a los servicios, pero
no justifica cortar el sistema entero si Redis cae.

## Rutas

El gateway no tiene lógica de negocio: solo autentica, limita y reenvía. Método, cuerpo, query
string e `Idempotency-Key` pasan sin cambios. Quita `Authorization` y cualquier `X-Client-Id`
que mande el cliente, y agrega `X-Client-Id`, `X-Correlation-Id` y `X-Forwarded-For`.

| Ruta pública | Servicio | Ruta interna | Timeout |
|---|---|---|---|
| `/api/v1/matriculas[/...]` | orquestador | `/matriculas[/...]` | 15 s |
| `/api/v1/sagas/...` | orquestador | `/sagas/...` | 5 s |
| `/api/v1/estudiantes[/...]` | inscripcion | `/estudiantes[/...]` | 5 s |
| `/api/v1/cursos[/...]` | bff-web | `/web/cursos[/...]` | 5 s |
| `/api/v1/web/...` | bff-web | `/web/...` | 5 s |
| `/health` | (el propio gateway, sin token) | — | — |

El timeout de `/api/v1/matriculas` es más largo porque cubre la Saga completa (3 pasos con sus
propios timeouts y reintentos). Debe quedar por encima del peor caso del orquestador; si B o C
cambian sus timeouts, revisen este valor (`gateway/app/main.py`, tabla `RUTAS`).

`/api/v1/cursos` pasa por el BFF porque Cupos habla gRPC y el gateway solo reenvía HTTP.
