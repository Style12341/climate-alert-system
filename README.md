# Sistema de Alertas Climáticas

Un usuario configura alertas sobre sus campos, con un evento climático (helada, lluvia, etc.) y un umbral. Un job compara periódicamente los pronósticos guardados con esos umbrales y notifica al usuario cuando corresponde.

> **[Guía para revisar el proyecto](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz)**
>
> El mejor lugar para entender el sistema, sus decisiones y el proceso de trabajo, con diagramas:
> [Resumen](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#resumen) ·
> [Arquitectura](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#arquitectura) ·
> [Flujo](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#flujo) ·
> [Modelo](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#modelo) ·
> [Decisiones](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#decisiones) ·
> [Escala](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#escala) ·
> [Proceso](https://claude.ai/artifact/7sANHK2Q4wZWWHGFnoRBPz#proceso)

## Cómo correrlo

Solo hace falta Docker con Compose. No hay nada que configurar.

```bash
docker compose up --build
```

Compose levanta PostgreSQL, aplica las migraciones, carga los datos simulados y arranca la API y el worker. Los secretos de `compose.yaml` son públicos y solo sirven para desarrollo.

Para recorrer todo el flujo con un solo comando (necesita `curl`, `npx` y `python3`):

```bash
scripts/smoke_test.sh
```

Para explorar la API, abrir <http://localhost:8000/docs>. Los tokens de cada usuario se obtienen con `docker compose logs seed`; se pegan en **Authorize**, sin la palabra `Bearer`.

Cómo correrlo sin Docker, los errores y otros detalles están en [`docs/operacion.md`](docs/operacion.md).

## Colección para Postman o Bruno

La carpeta `collection/` tiene la colección y su entorno, en formato Postman. Bruno los importa en ese mismo formato. Los pasos están en [`docs/operacion.md`](docs/operacion.md#usar-la-colección).

## Tests

Hacen falta [uv](https://docs.astral.sh/uv/) y Docker. Los tests levantan un PostgreSQL 17 real con testcontainers.

```bash
uv run pytest
```

## Decisiones principales

| Tema | Decisión | Motivo |
|------|----------|--------|
| [Forma del sistema](docs/Decisiones.md#1-forma-del-sistema-módulo-de-agrobot) | Módulo con procesos `api` y `worker` | Aprovecha los datos existentes sin sumar un servicio |
| [Job periódico](docs/Decisiones.md#3-job-periódico-apscheduler-3x) | APScheduler 3.x | Async nativo, sin broker, licencia MIT |
| [Datos climáticos](docs/Decisiones.md#4-datos-climáticos-una-fila-por-campo-día-y-evento) | Una fila por campo, día y evento | Es lo que describe el enunciado, y sumar eventos no requiere migraciones |
| [Duplicados](docs/Decisiones.md#7-deduplicación-una-notificación-por-alerta-fecha-y-canal) | Una notificación por alerta, fecha y canal | La base lo garantiza, incluso con varios workers |
| [Entrega](docs/Decisiones.md#8-entrega-outbox-con-entrega-al-menos-una-vez) | La notificación funciona como outbox | Se guarda antes de enviarse y se reintenta |
| [Volumen](docs/Decisiones.md#12-volumen-y-batching) | Evaluación en SQL por lotes de 5.000 alertas | La memoria no depende del volumen, medido a 24.000 campos |

Stack: Python 3.14, FastAPI, SQLAlchemy 2 async, PostgreSQL 17, Alembic, APScheduler 3.x y PyJWT.

## Lo que faltó

Lo que el enunciado pedía está hecho. Esto quedó sin hacer o sin probar:

- **Envío e ingesta reales.** Los dos son simulados: el enunciado excluye WhatsApp y pide mockear los datos.
- **Carga sostenida.** El benchmark es una sola corrida en una notebook, sobre PostgreSQL sin ajustes y sin envío. No se midió autovacuum con la cola funcionando durante horas.
- **Registro de envíos por lote.** Se registra fila por fila. Alcanza para unas 1.335 notificaciones por segundo, muy por encima del límite de 80 del proveedor.
- **Supuestos sin datos reales.** Las 3 alertas por campo son una estimación.
- **Bruno.** La importación se comprobó con su convertidor oficial, pero la colección no se ejecutó dentro de la aplicación.
- **Reprocesar desde la API.** Reencolar una notificación fallida requiere un [`UPDATE` manual](docs/operacion.md#reencolar-una-notificación-fallida).
- **Lo que rodea a un producto real.** Métricas, límite de pedidos, retención de `notifications`, revocación de tokens y otros temas están en [Simplificaciones frente a producción](docs/Decisiones.md#simplificaciones-frente-a-producción).

## Documentación

- [`docs/Decisiones.md`](docs/Decisiones.md): razonamiento de cada decisión, alternativas descartadas y casos borde.
- [`docs/ModeloDeDatos.md`](docs/ModeloDeDatos.md): esquema, reglas de la base y diagramas.
- [`docs/procedimiento.md`](docs/procedimiento.md): cómo se encaró el trabajo y qué cambió en el camino.
- [`docs/operacion.md`](docs/operacion.md): correrlo sin Docker, tokens, colección, errores, reencolado y scripts.
- [`docs/PRD.md`](docs/PRD.md), [`docs/SPEC.md`](docs/SPEC.md) y [`docs/PLAN.md`](docs/PLAN.md): documentos de trabajo, en inglés.
- [`statement.md`](statement.md): el enunciado.
