# Sistema de Alertas Climáticas

Un usuario configura alertas sobre sus campos, con un evento climático (helada, lluvia, etc.) y un umbral. Un job compara periódicamente los pronósticos guardados con esos umbrales y notifica al usuario cuando corresponde.

## Cómo funciona

1. El usuario crea una alerta sobre un campo, con un evento y un umbral.
2. La ingesta, simulada en este ejercicio, guarda una probabilidad por campo, día y evento.
3. Un job periódico compara los pronósticos con las alertas y crea las notificaciones, sin repetirlas.
4. Un dispatcher envía las notificaciones y reintenta si falla. Cada una queda guardada antes del primer intento de envío.

No hay integración con WhatsApp, tal como indica el enunciado. El envío está detrás de una interfaz con una implementación simulada.

## Cómo correrlo

Solo hace falta Docker con Compose. No hay nada que configurar.

```bash
docker compose up --build
```

Compose levanta PostgreSQL, aplica las migraciones, carga los datos simulados y arranca la API y el worker. La API queda en <http://localhost:8000>. El puerto se publica solo en `127.0.0.1`, porque el `JWT_SECRET` de `compose.yaml` es público.

`JWT_SECRET` y la contraseña de la base que aparecen en `compose.yaml` son solo para desarrollo. Son públicos: no se deben usar en ningún despliegue real. La aplicación no define un valor por defecto para ninguno de los dos.

Para borrar todo, incluidos los datos:

```bash
docker compose down -v
```

## Correrlo sin Docker

Hacen falta [uv](https://docs.astral.sh/uv/) y un PostgreSQL 17 con una base y un usuario que coincidan con `DATABASE_URL`.

1. Copiar `.env.example` a `.env` y completar `DATABASE_URL` y `JWT_SECRET`, de al menos 32 caracteres. El archivo lista todas las variables con sus valores por defecto.
2. Exportar las variables. La API, el worker y el comando de datos simulados leen `.env` del directorio actual, pero las migraciones solo leen el entorno (`migrations/env.py` usa `os.environ["DATABASE_URL"]`):

   ```bash
   set -a; . ./.env; set +a
   ```

3. Desde la raíz del repositorio, aplicar las migraciones, cargar los datos simulados y arrancar la API y el worker, cada uno en su terminal:

   ```bash
   uv run alembic upgrade head
   uv run python -m climate_alerts.agrobot.seed
   uv run uvicorn --factory climate_alerts.api.app:create_app --no-server-header
   uv run python -m climate_alerts.worker
   ```

La API queda en <http://localhost:8000>. El comando de datos simulados imprime los tokens en su salida.

## Obtener un token

La API se autentica con un JWT. El comando de datos simulados imprime uno por usuario:

```bash
docker compose logs seed
```

Cada línea tiene este formato:

```text
user 1 fields 1,3,5 token eyJhbGciOi...
```

Los tokens duran 24 horas. Para generar otros, ejecutar `docker compose run --rm seed`. Ese comando también actualiza los pronósticos.

## Probar la API en `/docs`

1. Abrir <http://localhost:8000/docs>.
2. Elegir **Authorize** y pegar el token, sin la palabra `Bearer`.
3. Probar `GET /api/v1/alerts` y `GET /api/v1/notifications`.

La primera evaluación corre unos segundos después del arranque. Si la lista de notificaciones está vacía, conviene esperar y repetir.

Compose habilita los dos canales de demostración, así que es esperable una fila `email` en `failed` para el usuario sin email.

Los decimales se devuelven como texto, por ejemplo `"70.00"`.

## Errores

Los errores usan la forma estándar de FastAPI con un `code` estable, pensado para que un cliente decida sin leer el mensaje:

```json
{"detail": "Alert not found", "code": "alert_not_found"}
```

Los errores de validación conservan la lista de FastAPI. Un evento inexistente usa la misma forma:

```json
{"detail": [{"type": "unknown_event_type", "loc": ["body", "event_type"], "msg": "Unknown event type"}]}
```

Los errores internos se registran en los logs y nunca se devuelven al cliente. Por eso `last_error`, en `GET /api/v1/notifications`, solo muestra un motivo de un conjunto fijo, como `Delivery failed`.

El catálogo completo de códigos está en la [sección 7 de `docs/SPEC.md`](docs/SPEC.md#7-api), y el razonamiento en la [decisión 25](Decisiones.md#25-manejo-de-errores).

## Usar la colección

La carpeta `collection/` recorre el flujo completo con validaciones. Tiene dos archivos:

- `climate-alerts.postman_collection.json`: la colección.
- `local.postman_environment.json`: el entorno.

**En Postman:** importar los dos archivos y seleccionar el entorno `Climate Alerts Local`.

**En Bruno:** importar la colección con **Import Collection** y elegir el formato Postman. Luego importar el entorno desde **Environments**, también en formato Postman, y seleccionarlo.

En cualquiera de los dos:

1. Copiar de la salida de `docker compose logs seed` el token del usuario 1 en `token_user_1`, y el del usuario 2 en `token_user_2`.
2. Esperar unos segundos después del arranque, para que corra la primera evaluación. La carpeta 6 espera sus notificaciones.
3. Ejecutar las carpetas en orden, porque las últimas usan el `alert_id` que captura `Create alert`.

La colección asume una base recién creada. Se puede volver a ejecutar, porque la carpeta 7 borra la alerta que creó. Si una ejecución se corta antes, `Create alert` responde `409` hasta que esa alerta se borre.

## Correr los tests

Hacen falta [uv](https://docs.astral.sh/uv/) y Docker. Los tests levantan un PostgreSQL 17 real con testcontainers.

```bash
uv run pytest
```

Los demás controles:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## Reencolar una notificación fallida

Una notificación `failed` no se reintenta sola y no hay un endpoint para reprocesarla. Se reencola con un `UPDATE`:

```bash
docker compose exec db psql -U climate -d climate_alerts -c "
UPDATE notifications
SET status = 'pending',
    attempts = 0,
    next_attempt_at = now(),
    last_error = NULL
WHERE id = 123 AND status = 'failed';"
```

`attempts` debe volver a `0`. Cada reserva suma un intento, y una fila que supera el máximo pasa a `failed` sin enviarse, así que una fila que quedó en el límite volvería directamente a `failed`.

## Scripts

| Script | Qué hace |
|--------|----------|
| `scripts/license_audit.py` | Lista la licencia declarada en PyPI de cada paquete de `uv.lock`. No reemplaza una revisión legal |
| `scripts/claim_forms.sql` | Compara las formas de la reserva de notificaciones en transacciones que se revierten. Necesita una base vacía y descartable: rechaza correr si la base ya tiene tablas. No mide el drenaje completo. El comando está en su encabezado |
| `scripts/volume_benchmark.py` | Mide las sentencias reales a 24.000 campos. **Vacía las tablas** de la base que recibe, así que rechaza cualquier host que no sea `localhost` o `127.0.0.1` |

Los resultados están en las decisiones [3](Decisiones.md#3-job-periódico-apscheduler-3x), [12](Decisiones.md#12-volumen-y-batching) y [14](Decisiones.md#14-estado-de-entrega-next_attempt_at-como-plazo-de-reserva).

## Stack

Python 3.14, FastAPI, SQLAlchemy 2 async, PostgreSQL 17, Alembic, APScheduler 3.x y PyJWT.

## Decisiones principales

| Tema | Decisión | Motivo |
|------|----------|--------|
| Forma del sistema | Módulo con procesos `api` y `worker` | Aprovecha los datos existentes sin sumar un servicio |
| Job periódico | APScheduler 3.x | Async nativo, sin broker, licencia MIT |
| Datos climáticos | Una fila por campo, día y evento | Es lo que describe el enunciado, y sumar eventos no requiere migraciones |
| Duplicados | Una notificación por alerta, fecha y canal | La base lo garantiza, incluso con varios workers |
| Canales | Una fila y un sender por canal | Un canal puede fallar sin afectar a los demás |
| Entrega | La notificación funciona como outbox | Se guarda antes de enviarse y se reintenta |
| Volumen | Evaluación en SQL por lotes de 5.000 alertas | La memoria no depende del volumen, medido a 24.000 campos |
| Autenticación | JWT que la API solo verifica | Sirve para un frontend y para servicios internos |

El razonamiento completo, las alternativas descartadas y los casos borde están en [Decisiones.md](Decisiones.md). El esquema y sus diagramas están en [ModeloDeDatos.md](ModeloDeDatos.md). El proceso de trabajo está en [procedimiento.md](procedimiento.md).

## Estado de los entregables

- [x] Código
- [x] Migraciones funcionales, con `upgrade` y `downgrade` desde una base vacía
- [x] Tests sobre PostgreSQL real
- [x] Instrucciones para correrlo
- [x] Decisiones documentadas
- [x] Lo que faltó, en la sección siguiente

## Lo que faltó

Lo que el enunciado pedía está hecho. Esto es lo que quedó sin hacer o sin probar:

- **Envío e ingesta reales.** Los dos son simulados: el enunciado excluye WhatsApp y pide mockear los datos.
- **Carga sostenida.** El benchmark es una sola corrida en una máquina, sin envío. No se midió el comportamiento de autovacuum con la cola funcionando durante horas.
- **Registro de envíos por lote.** El dispatcher registra cada resultado con un `UPDATE` propio. Drenar 1.152.000 notificaciones tardó 863 s, unas 1.335 por segundo, y 836,9 s de ese tiempo fueron el registro de resultados. Supera el límite de 80 mensajes por segundo del proveedor, por lo que no se optimizó.
- **Hardware de producción.** Los números salen de PostgreSQL sin ajustes en una notebook.
- **Supuestos sin datos reales.** Las 3 alertas por campo son una estimación.
- **Bruno.** La importación se comprobó con su convertidor oficial, pero no se ejecutó la colección dentro de la aplicación. En Postman se usó Newman, que es su ejecutor de línea de comandos.
- **Reprocesar desde la API.** Reencolar una notificación fallida requiere el `UPDATE` de esta página.
- **Lo que rodea a un producto real.** Métricas, límite de pedidos, retención de `notifications`, revocación de tokens y otros temas están en [Simplificaciones frente a producción](Decisiones.md#simplificaciones-frente-a-producción), con lo que haría falta en cada caso.
