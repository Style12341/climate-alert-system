# Sistema de Alertas Climáticas

> **Borrador.** La implementación todavía no empezó. Las instrucciones para correrlo se agregan cuando exista el código.

Permite que un usuario configure alertas sobre sus campos, indicando un evento climático (helada, lluvia, etc.) y un umbral. Un job compara periódicamente los pronósticos guardados con esos umbrales y notifica al usuario cuando corresponde.

## Cómo funciona

1. El usuario crea una alerta sobre un campo, con un evento y un umbral.
2. La ingesta, simulada en este ejercicio, guarda una probabilidad por campo, día y evento.
3. Un job periódico compara los pronósticos con las alertas y crea las notificaciones, sin repetirlas.
4. Un dispatcher envía las notificaciones y reintenta si falla. Cada una queda guardada antes del primer intento de envío.

No hay integración con WhatsApp, tal como indica el enunciado. El envío está detrás de una interfaz con una implementación simulada.

## Cómo correrlo

Pendiente: se completa con la implementación.

## Probar la API a mano

La carpeta `collection/` incluye una colección que recorre el flujo completo. Se abre en Postman y se puede importar en Bruno. Las instrucciones se completan con la implementación.

## Stack

FastAPI, SQLAlchemy, PostgreSQL, Alembic, APScheduler y PyJWT.

## Decisiones principales

| Tema | Decisión | Motivo |
|------|----------|--------|
| Forma del sistema | Módulo con procesos `api` y `worker` | Aprovecha los datos existentes sin sumar un servicio |
| Job periódico | APScheduler 3.x | Async nativo, sin broker, licencia MIT |
| Datos climáticos | Una fila por campo, día y evento | Es lo que describe el enunciado, y sumar eventos no requiere migraciones |
| Duplicados | Una notificación por alerta, fecha y canal | La base lo garantiza, incluso con varios workers |
| Canales | Una fila y un sender por canal | Un canal puede fallar sin afectar a los demás |
| Entrega | La notificación funciona como outbox | Se guarda antes de enviarse y se reintenta |
| Autenticación | JWT que la API solo verifica | Sirve para un frontend y para servicios internos |

El razonamiento completo, las alternativas descartadas y los casos borde están en [Decisiones.md](Decisiones.md). El esquema y sus diagramas están en [ModeloDeDatos.md](ModeloDeDatos.md). El proceso de trabajo está en [procedimiento.md](procedimiento.md).

## Estado de los entregables

- [ ] Código
- [ ] Migraciones funcionales
- [ ] Tests
- [ ] Instrucciones para correrlo

## Lo que faltó

Se completa al final, si corresponde.
