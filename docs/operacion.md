# Operación

Detalle operativo del sistema. La forma rápida de correrlo está en el [README](../README.md#cómo-correrlo).

## Correrlo sin Docker

Hacen falta [uv](https://docs.astral.sh/uv/) y un PostgreSQL 17 con una base y un usuario que coincidan con `DATABASE_URL`.

1. Copiar `example.env` a `.env` y completar `DATABASE_URL` y `JWT_SECRET`, de al menos 32 caracteres. El archivo lista todas las variables con sus valores por defecto.
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

## Datos de desarrollo en Compose

`JWT_SECRET` y la contraseña de la base que aparecen en `compose.yaml` son públicos y solo sirven para desarrollo: no se deben usar en ningún despliegue real. Por eso el puerto de la API se publica solo en `127.0.0.1`. La aplicación no define un valor por defecto para ninguno de los dos.

Para borrar todo, incluidos los datos:

```bash
docker compose down -v
```

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

## Usar la colección

La carpeta `collection/` recorre el flujo completo con validaciones. Está en formato Postman Collection v2.1, el único que abren las dos herramientas: Postman de forma nativa y Bruno con su [importador](https://docs.usebruno.com/get-started/import-export-data/postman-migration). Tiene dos archivos:

- `climate-alerts.postman_collection.json`: la colección.
- `local.postman_environment.json`: el entorno.

`scripts/smoke_test.sh` ejecuta todo el flujo con un solo comando: levanta el stack, lee los tokens y corre la colección con Newman.

**En Postman:** importar los dos archivos y seleccionar el entorno `Climate Alerts Local`.

**En Bruno:** importar la colección con **Import Collection** y elegir el formato Postman. Luego importar el entorno desde **Environments**, también en formato Postman, y seleccionarlo.

En cualquiera de los dos:

1. Copiar de la salida de `docker compose logs seed` el token del usuario 1 en `token_user_1`, y el del usuario 2 en `token_user_2`.
2. Esperar unos segundos después del arranque, para que corra la primera evaluación. La carpeta 6 espera sus notificaciones.
3. Ejecutar las carpetas en orden, porque las últimas usan el `alert_id` que captura `Create alert`.

La colección asume una base recién creada. Se puede volver a ejecutar, porque la carpeta 7 borra la alerta que creó. Si una ejecución se corta antes, `Create alert` responde `409` hasta que esa alerta se borre.

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

El catálogo completo de códigos está en la [sección 7 de `SPEC.md`](SPEC.md#7-api), y el razonamiento en la [decisión 16](Decisiones.md#16-manejo-de-errores).

## Scripts

| Script | Qué hace |
|--------|----------|
| `scripts/license_audit.py` | Lista la licencia declarada en PyPI de cada paquete de `uv.lock`. No reemplaza una revisión legal |
| `scripts/claim_forms.sql` | Compara las formas de la reserva de notificaciones en transacciones que se revierten. Necesita una base vacía y descartable: rechaza correr si la base ya tiene tablas. No mide el drenaje completo. El comando está en su encabezado |
| `scripts/volume_benchmark.py` | Mide las sentencias reales a 24.000 campos. **Vacía las tablas** de la base que recibe, así que rechaza cualquier host que no sea `localhost` o `127.0.0.1` |
| `scripts/smoke_test.sh` | Levanta el stack con Compose, espera la primera evaluación y el primer envío, y corre la colección con Newman. Deja el stack levantado. No es una prueba de carga ni reinicia la base: si hubo cambios manuales, ejecutar antes `docker compose down -v` |

Los resultados están en las decisiones [2](Decisiones.md#2-job-periódico-apscheduler-3x), [6](Decisiones.md#6-estado-de-entrega-next_attempt_at-como-plazo-de-reserva) y [10](Decisiones.md#10-volumen-y-batching).

## Integración continua

`.github/workflows/ci.yml` corre en cada push y pull request a `main`. Los trabajos son independientes y corren en paralelo:

| Trabajo | Herramienta | Falla cuando |
|---------|-------------|--------------|
| `lint` | `ruff check`, `ruff format --check` y `mypy src` | Hay un error de estilo, de formato o de tipos |
| `test` | `pytest` contra PostgreSQL 17 con Testcontainers | Falla una prueba |
| `dependencies` | `pip-audit` sobre `uv.lock` y `scripts/license_audit.py` | Hay una vulnerabilidad conocida, o una licencia ausente o copyleft sin revisar |
| `secrets` | gitleaks sobre todo el historial de git | Encuentra un secreto |
| `container` | Trivy sobre la imagen y el `Dockerfile` | Hay una vulnerabilidad CRITICAL o HIGH que ya tenga corrección, o una mala configuración CRITICAL o HIGH |
| `dast` | Compose, `scripts/smoke_test.sh` y ZAP, autenticado con el token de un usuario de prueba | Falla la colección, o ZAP reporta una alerta que `.zap/rules.tsv` no marque como IGNORE |
| `sast` | CodeQL para Python | El análisis no termina |

Con un archivo de reglas, ZAP corre su política activa completa (`Default Policy`) en lugar de `API-Minimal`, y su informe queda como artefacto de la ejecución. Los hallazgos de CodeQL aparecen en la pestaña Security de GitHub; en un repositorio privado requiere GitHub Advanced Security.

Dependabot (`.github/dependabot.yml`) propone cada semana actualizaciones de las dependencias de Python, de las acciones y de la imagen base.

Dependabot no actualiza lo que está fijado dentro de `ci.yml` como entrada de una acción o en un comando. Se actualiza a mano: los digests de las imágenes de ZAP y de gitleaks, la versión de Trivy, la versión de pip-audit y `UV_VERSION`.

La integración continua **no** despliega, no hace pruebas de carga y no ejecuta el sistema de forma sostenida.
