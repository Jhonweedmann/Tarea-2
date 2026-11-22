# ADR-006 · API Gateway propio y BFF web

**Estado:** aceptada

## Contexto

En la Tarea 1 había un solo servicio público (Matrículas) que autenticaba por su cuenta. En la
Unidad 3 el sistema pasa a tener cinco servicios (orquestador, inscripción, cupos, pagos y el BFF)
y la rúbrica pide un punto de entrada único que **enrute, autentique y limite**, más un BFF que
**agregue al menos dos servicios**. Además, la pantalla del alumno necesita datos de Inscripción,
Cupos y Pagos, y Pagos es una dependencia frágil: no puede tumbar la pantalla completa.

## Alternativas consideradas

**Gateway:**

- **Kong en modo DB-less:** plugins listos de auth (`key-auth`), rate limit con Redis y
  correlation id. Configuración declarativa en YAML. Pero agrega un componente que el equipo no
  conoce, y en la defensa ("explicar cualquier línea") gran parte del comportamiento quedaría
  dentro de plugins que no escribimos.
- **Nginx (`limit_req` + `auth_request`):** muy eficiente, pero el límite vive en la memoria de
  cada instancia (no es distribuido) y la autenticación por token necesita un subservicio aparte
  o scripting en Lua/njs. Las respuestas 401/429 en `problem+json` exigen configuración extra.
- **Gateway propio en FastAPI + httpx:** ~200 líneas, mismo stack que el resto, reutiliza el
  middleware de auth de la Tarea 1 (ADR-005) y se prueba con `unittest` como los demás servicios.
  A cambio, el rendimiento es menor que Nginx y cualquier funcionalidad extra hay que escribirla.

**Agregación para la pantalla del alumno:**

- **El cliente llama a cada servicio:** tres viajes de red desde el navegador, expone servicios
  internos y obliga al cliente a manejar las fallas de cada uno.
- **Agregar en el gateway:** mezcla lógica de presentación con enrutamiento y rompe la regla de
  que el gateway no tiene lógica de negocio.
- **BFF dedicado a la web:** un servicio que conoce lo que la pantalla necesita, llama en
  paralelo y decide qué hacer si una fuente falla.

## Decisión

1. **Gateway propio (`gateway/`)** como único servicio con puerto publicado (8080). Hace solo
   cuatro cosas, en este orden: asignar `X-Correlation-Id`, autenticar el Bearer token, aplicar
   el límite del cliente y reenviar según una tabla de prefijos. No conoce matrículas, cupos ni
   pagos.
2. **Un token por cliente** (`X-Client-Id`), cada uno con rol (lectura/escritura) y límite
   propio. Los servicios internos dejan de autenticar y confían en la red interna.
3. **Rate limit de ventana fija en Redis** (`INCR` + `EXPIRE` sobre `ratelimit:<cliente>:<ventana>`),
   `429` con `Retry-After`. **Fail-open** si Redis no responde.
4. **BFF (`bff-web/`)** con `GET /web/alumnos/{id}/panel`: cuatro llamadas en paralelo, cada una
   con su tiempo máximo (2 s), y **degradación parcial**: si Pagos, Cupos o la lista de
   inscripciones fallan, su sección sale como `{"disponible": false, "motivo": ...}` y el panel
   responde `200`. Solo el estudiante es obligatorio.
5. Rutas públicas bajo `/api/v1/...`, siguiendo ADR-003 (versión en la ruta).

## Justificación

- **Por qué el gateway no tiene lógica de negocio:** si el gateway supiera, por ejemplo, que una
  matrícula necesita cupo, cualquier cambio de negocio obligaría a desplegarlo, y es el único
  componente por el que pasa todo el tráfico. Su tabla de rutas solo traduce prefijos
  (`/api/v1/sagas/x` → `orquestador:/sagas/x`) y reescribe `Location` de vuelta, igual que haría
  `proxy_pass` + `proxy_redirect` en Nginx.
- **Por qué auth central:** un solo lugar donde cambiar el esquema (p. ej. a JWT) y donde
  auditar quién llama. El gateway **descarta** cualquier `X-Client-Id` que mande el cliente,
  así nadie puede hacerse pasar por otro.
- **Por qué ventana fija y no token bucket:** es la versión más simple de explicar y de mostrar en
  vivo (25 solicitudes → 20 pasan, 5 reciben 429). Su defecto conocido es que en el borde entre
  dos ventanas un cliente puede enviar hasta el doble del límite en poco tiempo; para proteger
  servicios internos de un abuso sostenido es suficiente.
- **Por qué fail-open:** el límite protege a los servicios de un cliente abusivo, pero si Redis
  cae, rechazar a todos los clientes convierte una falla secundaria en una caída total. Se acepta
  el riesgo de pasar unos segundos sin límite. (La idempotencia de la Saga, en cambio, **sí**
  falla cerrada, porque ahí el riesgo es cobrar dos veces.)
- **Por qué degradación parcial en el BFF:** la regla de negocio prohíbe inscribir sin cobro, pero
  **mostrar** el panel sin la sección de pagos no rompe ninguna regla. Las llamadas en paralelo
  hacen que el panel tarde lo que tarda la fuente más lenta, acotada a 2 s, y no la suma.

## Costo aceptado

- El gateway es un punto único de falla y un salto de red extra en cada solicitud. Se mitiga con
  healthcheck y con que es stateless (se pueden levantar réplicas: el límite ya está en Redis).
- Mantener código propio para algo que Kong o Nginx traen hecho.
- Los servicios internos no autentican: si alguien entra a la red interna, puede llamarlos
  directamente. Aceptable en este alcance; en producción se agregaría mTLS entre servicios.
- Con el contenedor de Pagos detenido, el BFF espera los 2 s completos: en las pruebas la
  llamada terminó por timeout y no por conexión rechazada (en la Tarea 1 se observó lo mismo y se
  atribuyó a la resolución del nombre del contenedor detenido). El circuit breaker de C resuelve
  esto para el orquestador; el BFF podría reutilizarlo más adelante.

## Consecuencias

- `docs/contratos/gateway.md` y `docs/contratos/bff-web.md` son el contrato público.
- Métricas: `http_requests_total{service="gateway",status="429"}`, `gateway_rate_limited_total{cliente}`
  y `bff_degradaciones_total{dependencia,motivo}`, visibles en Grafana.
- Evidencia: 401 sin token, 429 al superar el límite (y otro cliente sin afectar) y panel del BFF
  con Pagos caído (ver README).
