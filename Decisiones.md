# Decisiones de diseño

> **Borrador vivo.** Se edita a medida que avanza el trabajo. Cada decisión indica si está cerrada o abierta.

Este documento explica qué se decidió, por qué, qué se descartó y qué límites acepta cada elección. El criterio general es mantenerse fiel al enunciado y preferir soluciones probadas, sin abstracciones que no resuelvan un problema concreto.

## Resumen

| # | Tema | Decisión | Estado |
|---|------|----------|--------|
| 1 | Forma del sistema | Módulo de Agrobot: un código, una base, procesos `api` y `worker` | Cerrada |
| 2 | Agrobot | Mock propio con datos simples y factibles | Cerrada |
| 3 | Job periódico | APScheduler 3.x en un proceso propio | Cerrada |
| 4 | Datos climáticos | Una fila por campo, día y evento | Cerrada |
| 5 | Clave del pronóstico | `field_id` | Cerrada |
| 6 | Granularidad | Campo, no lote | Cerrada |
| 7 | Deduplicación | Una notificación por alerta, fecha y canal | Cerrada |
| 8 | Entrega | La notificación funciona como outbox, con entrega al menos una vez | Cerrada |
| 9 | Umbral | `>=` sobre `NUMERIC(5,2)` de 0 a 100 | Cerrada |
| 10 | Fecha actual | Zona horaria IANA por campo | Cerrada |
| 11 | Tests | PostgreSQL real | Cerrada |
| 12 | Volumen y batching | Evaluación por lotes de 5.000 alertas, entrega por lotes de 500 notificaciones | Cerrada |
| 13 | Parámetros operativos | Valores por defecto en código, sobrescribibles por variable de entorno | Cerrada |
| 14 | Estado de entrega | `next_attempt_at` funciona como plazo de reserva, sin estado `sending` | Cerrada |
| 15 | Alertas | Sin `user_id` ni pausa, una vigente por campo y evento | Cerrada |
| 16 | Catálogo de eventos | Tabla de referencia con clave de texto | Cerrada |
| 17 | Borrado de alertas | Borrado lógico con `deleted_at` | Cerrada |
| 18 | Autenticación | JWT que la API solo verifica | Cerrada |
| 19 | API REST | Alertas y notificaciones, siempre filtradas por dueño | Cerrada |
| 20 | Datos simulados | Un comando único, idempotente y determinista | Cerrada |
| 21 | Canales de notificación | Una fila por canal y un sender por canal | Cerrada |
| 22 | Colección para pruebas manuales | Postman Collection v2.1, importable en Bruno | Cerrada |
| 23 | Construcción de consultas | Lenguaje de expresiones de SQLAlchemy 2.0 | Cerrada |

El detalle de cada tabla y sus diagramas están en [ModeloDeDatos.md](ModeloDeDatos.md).

---

## 1. Forma del sistema: módulo de Agrobot

**Decisión:** el sistema se construye como un módulo dentro de Agrobot. Hay un solo código, una sola base PostgreSQL y una sola imagen, que corre dos procesos: `api` y `worker`.

**Por qué:**

- El enunciado pide aprovechar datos "ya disponibles" en la base.
- Las claves foráneas reales hacia campos y usuarios protegen la integridad.
- Para un equipo chico, un servicio adicional agrega costo operativo sin un beneficio concreto.

**Alternativas descartadas:**

| Alternativa | Por qué no |
|-------------|------------|
| Servicio separado sobre la misma base | Dos códigos serían dueños de un mismo esquema y tendrían que coordinar migraciones |
| Microservicio con base propia | Obliga a copiar o sincronizar pronósticos y campos, lo contrario de aprovechar los datos existentes |

**El worker no corre dentro de FastAPI.** Con varias réplicas de la API habría varios planificadores, y cada deploy reiniciaría el job.

**Límite aceptado:** la frontera del módulo depende de la disciplina del código. Extraerlo más adelante requiere trabajo.

## 2. Agrobot es un mock propio

**Decisión:** Agrobot no existe como sistema accesible, así que las tablas de usuarios, campos y pronósticos son una versión mínima creada para este ejercicio, con datos simulados pero factibles.

**Por qué:** el enunciado pide mockear los datos meteorológicos. El foco del trabajo es el sistema de notificaciones, por lo que el mock se mantiene simple.

## 3. Job periódico: APScheduler 3.x

**Decisión:** el job corre con `AsyncIOScheduler` de APScheduler 3.x en un proceso propio. La versión se fija por debajo de 4.

**Por qué:**

- Ejecuta funciones `async def` directamente en el event loop, sin adaptadores.
- Ofrece `max_instances` y `coalesce`, así que no hace falta programar esa lógica a mano.
- No agrega infraestructura: ni broker ni tablas propias.
- Solo suma dos paquetes, ambos con licencia MIT.
- Tiene historia larga (primera versión en 2009) y mantenimiento activo (3.11.3 en junio de 2026).

**Auditoría de librerías** (datos del 8 de noviembre de 2026):

| Librería | Licencia | Descargas/mes | Contribuidores | Última versión |
|----------|----------|---------------|----------------|----------------|
| Celery | BSD-3-Clause | 43,2 M | 1.628 | 5.7.0 |
| Temporal (SDK) | MIT | 38,1 M | 124 | 1.35.0 |
| **APScheduler** | **MIT** | **34,4 M** | **74** | **3.11.3** |
| Huey | MIT | 12,9 M | 72 | 3.5.4 |
| RQ | BSD-2-Clause | 9,2 M | 381 | 3.0.0 |
| arq | MIT | 3,9 M | 67 | 0.28.0 |
| Taskiq | MIT | 2,1 M | 87 | 0.13.0 |
| Procrastinate | MIT | 1,9 M | 81 | 3.10.0 |
| Dramatiq | LGPL-3.0 | 1,6 M | 132 | 2.2.1 |
| PgQueuer | MIT | 0,5 M | 22 | 1.6.0 |

**Cómo se hizo:** se resolvió el árbol completo de dependencias de cada candidata con `uv pip compile` para Python 3.13, y se leyó la licencia declarada de cada paquete en PyPI. Las licencias que GitHub no identificó (Celery y RQ) se confirmaron leyendo el archivo `LICENSE`.

**Hallazgos de licencia:**

- APScheduler depende solo de `tzlocal`, también MIT.
- Celery y sus 16 dependencias tienen licencias permisivas.
- Procrastinate es MIT, pero depende de `psycopg` y `psycopg-pool`, que son LGPL-3.0.
- Dramatiq es LGPL-3.0 como dependencia directa.
- Huey y `taskiq-dependencies` no declaran licencia en su metadata de PyPI, pero sus archivos `LICENSE` son MIT.

**Por qué no las otras finalistas:**

| Librería | Motivo |
|----------|--------|
| Celery | Es la más probada y su licencia no es un problema. No tiene tareas `async def` nativas (confirmado en 5.6, sin novedades en las notas de 5.7). Usar `asyncio.run` por tarea obliga a desactivar el pool de conexiones, según la documentación de SQLAlchemy. Además agrega un broker y un proceso beat, que debe ser único para no duplicar tareas. Los reintentos que ofrece ya viven en la tabla de notificaciones |
| Procrastinate | Técnicamente es la más cercana y garantiza una ejecución por período desde la base. Tiene cerca de 18 veces menos descargas que APScheduler y su proyecto busca mantenedores. Sería un segundo registro del estado de entrega, y su esquema debe integrarse a mano con Alembic |
| APScheduler 4 | Sigue en alfa y su README pide no usarlo en producción |
| arq | Está en modo de solo mantenimiento |

**Límites aceptados:**

- APScheduler solo planifica: cada réplica dispara por su cuenta. Es seguro porque las garantías están en PostgreSQL (decisión 7).
- No se usa un job store persistente, porque la documentación indica que no se puede compartir entre procesos. Los jobs se definen en código.
- Al apagarse, su ejecutor asyncio cancela los jobs en curso y no puede esperarlos, aun con `wait=True`. El worker los deja terminar por su cuenta antes de apagar el planificador ([especificación](docs/SPEC.md#10-worker-lifecycle)).
- Las descargas incluyen instalaciones de CI y transitivas. No se encontraron casos empresariales citables para ninguna opción, así que descargas y contribuidores son indicadores aproximados.

**Revisión funcional de las demás opciones**, según su documentación:

| Librería | Infraestructura | `async def` nativo | Tareas periódicas | Por qué no |
|----------|-----------------|--------------------|-------------------|------------|
| RQ | Redis o Valkey | No se documenta | Un solo scheduler por cola. Su cron sigue en beta | Agrega Redis |
| Huey | Admite PostgreSQL con `PostgresHuey` | No: "does not support a full asyncio pipeline" | Solo un consumidor debe encolarlas | No encaja en un stack async |
| Taskiq | Broker externo. El de PostgreSQL es de terceros | Sí | Una única instancia del scheduler | Agrega un broker y sigue en versión 0.x |
| PgQueuer | Solo PostgreSQL | Sí | Cron integrado | Encaja técnicamente, pero es la opción con menos adopción (22 contribuidores) y sería un segundo registro del estado de entrega |
| Temporal | Un servicio Temporal propio | Sí, aunque sus actividades son síncronas por defecto | Schedules del servicio | Un servidor completo para dos jobs periódicos |

Ninguna cambia la elección. PgQueuer y Procrastinate son las alternativas técnicamente más cercanas. Pierden por adopción y porque duplicarían una cola que el modelo de datos ya resuelve.

## 4. Datos climáticos: una fila por campo, día y evento

**Decisión:**

```text
weather_forecasts
  field_id, forecast_date, event_type   -- clave primaria
  probability  NUMERIC(5,2)             -- 0 a 100
  fetched_at   TIMESTAMPTZ
```

**Por qué:** el enunciado dice que "para cada día en el futuro hay una entrada por cada evento climático y su probabilidad". Sumar un evento nuevo es agregar filas, y una sola consulta evalúa todos los tipos.

**Qué devuelven los proveedores:**

| Proveedor | Campos de probabilidad | Escala | ¿Probabilidad de helada? | Forma |
|-----------|------------------------|--------|--------------------------|-------|
| Open-Meteo | `precipitation_probability_max/mean/min` | % | No, solo `temperature_2m_min` | Ancha |
| OpenWeather One Call 3.0 | `daily.pop` | 0 a 1 | No, solo `temp.min` | Ancha |
| WeatherAPI | `daily_chance_of_rain`, `daily_chance_of_snow` | % entero | No, solo `mintemp_c` | Ancha |
| Tomorrow.io | `precipitationProbability`, `thunderstormProbability` | % | No | No documentada |
| Visual Crossing | `precipprob` | 0 a 100 | No, solo `tempmin` | Ancha |
| AccuWeather (documentación de terceros) | Lluvia, nieve, hielo, precipitación | % entero | No | Por día y noche |

**Qué implica:**

| Hallazgo | Consecuencia |
|----------|--------------|
| Los proveedores devuelven una fila ancha por día | La ingesta transforma el formato al de una fila por evento |
| Ninguno informa probabilidad de helada | Se deriva, por ejemplo de un ensamble (ECMWF publica la probabilidad de temperatura bajo 0 °C). El mock cumple ese rol |
| Las escalas difieren | Se normaliza a 0 a 100 en la ingesta |
| "Lluvia" no significa lo mismo: Open-Meteo cuenta desde 0,1 mm y Tomorrow.io desde 0,0254 cm | Limitación documentada |
| Las respuestas documentadas no incluyen la hora de corrida del modelo | Se guarda `fetched_at` y se actualiza la fila, sin historial |

El valor diario representa el **pico de probabilidad del día**. La notificación guarda una copia de lo observado, así que no hace falta conservar el historial de pronósticos.

**Alternativa descartada:** una columna por evento. Cada evento nuevo exigiría una migración y un cambio en el evaluador.

## 5. Clave del pronóstico: `field_id`

**Decisión:** los pronósticos se asocian directamente al campo.

**Por qué:** es el modelo más simple, coincide con "sus campos" del enunciado y la ingesta está fuera de alcance.

**Costo aceptado:** dos campos vecinos en la misma celda del modelo duplican filas. No es un caso necesariamente raro: en una celda de 9 km entran unos 13 campos promedio de Argentina. La frecuencia real depende de dónde estén los clientes, un dato que no tenemos.

**Evolución posible:** una ingesta real puede agrupar las llamadas al proveedor por coordenadas redondeadas y escribir una fila por campo. Así, el único costo restante es de almacenamiento. Si eso importara, la tabla compartida `weather_locations` es el siguiente paso.

## 6. Granularidad: campo, no lote

**Decisión:** las alertas se configuran por campo. Los lotes no se modelan.

**Por qué:** el enunciado habla de "alertas sobre sus campos", y una alerta por lote prometería una precisión que los datos no tienen.

| Unidad | Superficie |
|--------|------------|
| Celda de 9 km (ECMWF) | 8.100 ha |
| Celda de 13 km (GFS, ICON) | 16.900 ha |
| Argentina, explotación promedio (CNA 2018) | unas 627 ha |
| Uruguay, explotación mediana (2011) | 361 ha |
| México, superficie agrícola promedio por unidad (2022) | 8,8 a 14,7 ha |
| Paraguay, promedio del 1 % de explotaciones más grandes (censo de 1981) | unas 7.300 ha |

Open-Meteo devuelve el centro de la celda, así que los lotes de una misma celda recibirían el mismo valor. Además, las heladas pueden variar en distancias de metros a cientos de metros según el terreno.

**Límites de la evidencia:** no se encontraron datos de Brasil o Colombia, tamaños de lotes ni cobertura de modelos de alta resolución en Sudamérica. Open-Meteo integra modelos de hasta 1 km en algunas regiones. Si cubrieran estos países, habría que revisar esta decisión.

## 7. Deduplicación: una notificación por alerta, fecha y canal

**Decisión:** una restricción `UNIQUE (alert_id, forecast_date, channel)`, con inserción mediante `INSERT ... SELECT ... ON CONFLICT DO NOTHING`.

**Por qué:** el job corre de forma periódica y la condición puede seguir vigente durante varias corridas. La garantía vive en la base, así que resiste réplicas concurrentes sin coordinación en la aplicación.

La notificación guarda la probabilidad y el umbral del momento, porque la ingesta puede sobrescribir el pronóstico después.

## 8. Entrega: outbox con entrega al menos una vez

**Decisión:** la fila de la notificación es también el registro de entrega. Un dispatcher separado toma filas pendientes con `FOR UPDATE SKIP LOCKED`, intenta enviarlas fuera de la transacción y reintenta si falla.

**Por qué:** cada notificación queda guardada antes de cualquier intento de envío. Si el proceso se cae, la fila sigue ahí.

**Límite aceptado:** un corte entre el envío y su registro puede producir un envío duplicado. Sin idempotencia en el proveedor, la entrega exacta no es alcanzable a través de la red.

No se integra WhatsApp, como indica el enunciado. El envío queda detrás de una interfaz con una implementación simulada.

## 9. Umbral: `>=` sobre `NUMERIC`

**Decisión:** se notifica cuando la probabilidad es mayor o igual al umbral. Los valores usan `NUMERIC(5,2)`.

**Por qué:** el enunciado dice tanto "a partir del cual" como "se superan". Se eligió la primera formulación porque describe cómo un usuario define su límite. `NUMERIC` evita los errores de comparación propios de los números de coma flotante.

## 10. Fecha actual: zona horaria por campo

**Decisión:** cada campo tiene una zona horaria IANA, y "hoy" se calcula en SQL según esa zona.

**Por qué:** FieldData opera en seis países. Usar la fecha UTC daría un día equivocado durante parte de la jornada.

## 11. Tests con PostgreSQL real

**Decisión:** los tests corren contra PostgreSQL, no SQLite.

**Por qué:** `ON CONFLICT`, `SKIP LOCKED` y las conversiones de zona horaria son justamente el comportamiento a verificar.

## 12. Volumen y batching

**Decisión:** evaluar con una sola sentencia SQL por lote de 5.000 alertas, cada lote en su propia transacción, y entregar en lotes de 500 notificaciones. La sentencia usa solo `ON CONFLICT DO NOTHING`, sin `NOT EXISTS`.

**Estimación:** FieldData declara más de 12.000 campos y se espera duplicarlos en un año.

| Magnitud | Cálculo | Resultado |
|----------|---------|-----------|
| Filas de pronóstico | 24.000 campos × 16 días × 7 eventos | 2.688.000 |
| Alertas (supuesto: 3 por campo) | 24.000 × 3 | 72.000 |
| Peor caso de notificaciones | 72.000 × 16 días | 1.152.000 |

**Dato de contexto:** Meta documenta 80 mensajes por segundo por número como valor por defecto, y responde con el error `130429` al excederlo. A ese ritmo, 21.600 notificaciones tardarían 4,5 minutos, y el peor caso, 4 horas.

**Medición:** benchmark descartable a 24.000 campos, con PostgreSQL 17 sin ajustes, 28 núcleos, 31 GiB de memoria y envío simulado.

| Escenario | Tiempo | WAL | Memoria del cliente |
|-----------|--------|-----|---------------------|
| Ninguna alerta supera el umbral | 1 a 2 s | 0 | 36 MiB |
| Frente frío, 21.600 notificaciones | 1,1 s | 8,6 MiB | 36 MiB |
| Peor caso, 1.152.000 notificaciones en una transacción | 9,7 a 10,9 s | 445 MiB | 37 MiB |
| Peor caso en lotes de 5.000 alertas | 10,0 s (lote más lento: 0,83 s) | 443 MiB | 36 MiB |
| Peor caso en lotes de 1.000 alertas | 18,2 s (lote más lento: 0,45 s) | 444 MiB | 36 MiB |
| Peor caso cargando los candidatos en Python | 19,6 s | 445 MiB | **506 MiB** |
| Entrega en lotes de 500, 1 dispatcher | 34 a 40 s | 1 GiB | 39 MiB |
| Entrega en lotes de 500, 4 dispatchers | 17,3 s | 1 GiB | 43 MiB |
| Cargar todos los pendientes en Python | 0,9 s | 0 | **399 MiB** |

**Evaluación:**

- La memoria no depende del volumen, porque ninguna fila llega a Python.
- Los lotes de 5.000 cuestan lo mismo en total que una sola transacción, pero cada transacción dura menos de un segundo. Si algo falla, solo se repite ese lote.
- Los lotes no reducen el WAL. Los de 1.000 tardan casi el doble y escriben 860 MiB en archivos temporales, un efecto que no se investigó.
- Con 4 evaluadores simultáneos sobre los mismos datos no hubo duplicados ni deadlocks. Tampoco hubo mejora de velocidad: las réplicas aportan resistencia, no capacidad.

**Por qué no se usa `NOT EXISTS`:** parecía una optimización inocua, pero hace que el rendimiento dependa de las estadísticas de la tabla.

| Estado de `notifications` | Variante | Plan | Tiempo |
|---------------------------|----------|------|--------|
| Analizada vacía, 4 evaluadores | `NOT EXISTS` sin `ORDER BY` | Nested Loop Anti Join sobre Seq Scan | **1.653,8 s** |
| Analizada vacía, 4 evaluadores | `NOT EXISTS` con `ORDER BY` | Merge Anti Join | 15,8 s |
| Sin analizar, 4 evaluadores | `NOT EXISTS` sin `ORDER BY` | Hash Anti Join | 16,0 s |
| Analizada vacía, 1 evaluador | `NOT EXISTS` sin `ORDER BY` | Nested Loop Anti Join | 38,0 s |
| Analizada vacía, 1 evaluador | Solo `ON CONFLICT` | Sin anti join | 9,9 s |

Un sistema recién desplegado tiene esa tabla vacía justo en su primera corrida grande. Agregar `ORDER BY` evitó el problema, pero no lo garantiza. Sin el anti join, el problema no existe.

**Costo aceptado:** cuando todas las notificaciones ya existen, una corrida tarda 2,74 s en vez de 1,35 s y consume 1.152.000 valores de la secuencia. Con `bigint`, aun en el peor caso cada 5 minutos, la secuencia dura unos 76 millones de años.

**`ORDER BY` se mantiene** para que todas las réplicas inserten en el mismo orden. Cero deadlocks en la medición no prueba que omitirlo sea seguro.

**Entrega:**

1. Se toman hasta 500 filas en una transacción corta y se marcan con un plazo de reserva.
2. El envío ocurre fuera de la transacción, con concurrencia limitada.
3. Se registra el resultado de cada fila.

La base no es el cuello de botella: procesó entre 28.900 y 68.100 filas por segundo con envío simulado, mientras que el límite real es el del proveedor.

**Qué significa "no perder notificaciones":**

| Momento del fallo | Qué ocurre |
|-------------------|------------|
| Durante la evaluación | El lote se revierte y la siguiente corrida lo recalcula desde los pronósticos |
| Después de confirmar el lote | La fila queda guardada hasta llegar a enviada o fallida. Nunca se borra |
| Durante el envío | Vence la reserva y otro dispatcher la retoma |
| Se agotan los reintentos | Queda como fallida y visible, disponible para reprocesar |

**Límites de esta medición:**

- La combinación exacta propuesta (`ON CONFLICT` con `ORDER BY`, sin anti join) no se midió. Se verifica en la implementación.
- El supuesto de 3 alertas por campo no tiene respaldo en datos reales.
- Las colas no se probaron de forma sostenida: autovacuum no llegó a correr durante las pruebas de entrega.
- Una entrega escribe cerca de 1 GiB de WAL por cada 1,15 millones de filas, porque `status` forma parte del índice parcial. Con volúmenes normales no es relevante.
- Si el worker está caído hasta que pasa la fecha del pronóstico, ese día ya no se evalúa.

## 13. Parámetros operativos

**Decisión:** los valores que dependen de la operación son parámetros. Tienen un valor por defecto en el código y se pueden sobrescribir con una variable de entorno.

**Por qué:** los tamaños de lote salieron de una medición en una máquina concreta y con supuestos propios. En otro entorno deben poder ajustarse sin cambiar el código. Las variables de entorno son la opción más simple y no agregan infraestructura.

| Variable | Valor por defecto | Origen |
|----------|-------------------|--------|
| `EVALUATION_CHUNK_SIZE` | 5.000 alertas | Benchmark (decisión 12) |
| `DISPATCH_BATCH_SIZE` | 500 notificaciones | Benchmark (decisión 12) |
| `NOTIFICATION_CHANNELS` | `whatsapp` | Decisión 21. Un solo canal mantiene válidos los volúmenes medidos |

Los intervalos, reintentos y plazos de reserva están en la [especificación](docs/SPEC.md#4-configuration).

**Excepción:** los secretos, como `JWT_SECRET`, no tienen valor por defecto. Si faltan, la aplicación no arranca, para que nunca se despliegue con un secreto conocido.

## 14. Estado de entrega: `next_attempt_at` como plazo de reserva

**Decisión:** reservar una fila consiste en mover `next_attempt_at` hacia el futuro. Los estados guardados son solo `pending`, `sent` y `failed`.

**Por qué:** si un proceso cae, la fila vuelve a estar disponible sola, sin un proceso de recuperación ni un estado intermedio. Es el mismo modelo de visibilidad que usan Amazon SQS y pgmq.

**Alternativa descartada:** un estado `sending` con `locked_until`. Agrega un estado y obliga a recuperar las filas que quedaron a medio camino.

**Reintentos:** backoff exponencial con jitter, en una sola capa. Los errores permanentes pasan directo a `failed`. Los intentos se cuentan al reservar, así que una fila que hace caer al worker termina por detenerse.

**Límite real del lote:** `UPDATE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` puede reservar más de `n` filas con ciertos planes, porque PostgreSQL puede volver a ejecutar la subconsulta. Se reprodujo en PostgreSQL 17 con estadísticas desactualizadas. Por eso la selección se hace en una CTE `MATERIALIZED`, que se evalúa una sola vez.

**Límite aceptado:** esta reserva difiere levemente de la que se midió en el benchmark, por lo que se vuelve a verificar en la implementación.

## 15. Alertas: sin `user_id` y sin pausa

**Decisión:** la alerta pertenece a un campo y el dueño se obtiene a través de él. Solo puede haber una alerta vigente por campo y evento.

**Por qué:** guardar también el usuario crearía dos fuentes de verdad. El enunciado habla de "sus campos", y un usuario puede tener varios. Pausar alertas no forma parte de lo pedido.

**Evolución posible:** si un campo necesitara varios destinatarios, se agregaría esa relación.

**Umbral mayor que 0:** un umbral de 0 notificaría todos los días.

## 16. Catálogo de eventos: tabla de referencia

**Decisión:** una tabla `climate_event_types` con el código como clave.

**Por qué:** el enunciado dice "helada, lluvia, etc.", es decir, una lista abierta. Agregar un evento es agregar una fila, y la clave de texto evita un join para leer una alerta.

**Alternativa descartada:** un enum de PostgreSQL. La documentación oficial indica que no se pueden quitar valores sin recrear el tipo.

## 17. Borrado de alertas: lógico

**Decisión:** borrar una alerta completa `deleted_at`. Deja de evaluarse, y su historial se conserva.

**Por qué:** el borrado físico en cascada destruiría notificaciones pendientes e historial. Eso contradice el requisito de no perder notificaciones.

**Alternativa descartada:** impedir el borrado cuando la alerta tiene notificaciones. Una alerta que disparó alguna vez no podría eliminarse nunca.

**Consecuencias aceptadas:**

- Toda lectura de alertas filtra las borradas, y la unicidad necesita un índice parcial.
- Las notificaciones ya creadas se entregan aunque la alerta se borre después.
- Borrar y recrear una alerta genera un identificador nuevo, así que puede notificar otra vez las mismas fechas.

## 18. Autenticación: JWT que la API solo verifica

**Decisión:** cada pedido envía `Authorization: Bearer <token>`. La API valida la firma y la expiración, y toma el usuario del claim `sub`. No hay login ni contraseñas: los tokens los emite Agrobot, y en este ejercicio los genera un script de desarrollo.

**Por qué:** el enunciado admite tanto un frontend como servicios internos. Un JWT sirve para ambos, y es el camino estándar del [tutorial de seguridad de FastAPI](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/), que usa PyJWT y una dependencia `get_current_user`.

**Alternativas descartadas:**

| Alternativa | Por qué no |
|-------------|------------|
| Clave de servicio y `X-User-Id` | Una clave no puede viajar a un navegador. Además, el usuario sería un dato que el llamador afirma, sin verificación |
| Usuario en la ruta, sin autenticación | Nada impondría el "sus campos" del enunciado |
| Login completo con `OAuth2PasswordBearer` | Exige un `tokenUrl`, y emitir credenciales corresponde a Agrobot |

**Cómo se implementa:**

- `HTTPBearer` de FastAPI, y toda la identidad resuelta en una sola dependencia.
- El algoritmo se fija por configuración y nunca se toma del encabezado del token, como indica [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html).
- `sub` y `exp` son obligatorios. Cualquier otro caso responde `401`.
- El secreto debe tener al menos 256 bits y no puede ser una contraseña memorizable.

PyJWT 2.15 tiene licencia MIT y no agrega dependencias obligatorias en la versión de Python usada.

## 19. API REST

| Endpoint | Función |
|----------|---------|
| `POST /api/v1/fields/{field_id}/alerts` | Crea una alerta |
| `GET /api/v1/alerts?field_id=` | Lista las alertas del usuario |
| `GET /api/v1/alerts/{id}` | Devuelve una alerta |
| `PATCH /api/v1/alerts/{id}` | Cambia el umbral |
| `DELETE /api/v1/alerts/{id}` | Borra lógicamente la alerta |
| `GET /api/v1/notifications?status=&channel=&field_id=&cursor=&limit=` | Historial paginado |
| `GET /api/v1/climate-event-types` | Catálogo de eventos |
| `GET /health/live`, `GET /health/ready` | Estado del proceso y de la base |

| Situación | Respuesta |
|-----------|-----------|
| Token ausente, inválido o vencido | `401` |
| Recurso inexistente o de otro usuario | `404` |
| Ya existe una alerta vigente para ese campo y evento | `409` |
| Evento inexistente, umbral fuera de rango o `limit` excesivo | `422` |

**Pertenencia:** cada consulta filtra por dueño, como exige [OWASP API1:2023](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/). Se responde `404` para no revelar si el recurso existe. Es una práctica extendida, aunque no hay consenso absoluto entre `403` y `404`.

**Tamaño de página:** `limit` se valida en el servidor, como recomienda [OWASP API4:2023](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/).

Usuarios y campos no tienen endpoints propios porque pertenecen a Agrobot.

## 20. Datos simulados: un comando único

**Decisión:** un comando corre una sola vez después de las migraciones, mediante [`service_completed_successfully`](https://docs.docker.com/compose/how-tos/startup-order/) de Docker Compose. Es idempotente y determinista, y genera fechas relativas al día actual. Volver a ejecutarlo actualiza los pronósticos, lo que simula un nuevo ciclo de ingesta.

**Qué crea:** unos pocos usuarios con varios campos en distintas zonas horarias, y un escenario fijo de helada que supera un umbral, para que la primera evaluación produzca notificaciones visibles. También imprime un JWT por usuario para probar la API desde `/docs`.

**Alternativas descartadas:**

| Alternativa | Por qué no |
|-------------|------------|
| Ingesta simulada periódica en el worker | Es un job más para construir y probar, y los cambios aleatorios harían que la demostración no fuera reproducible. El enunciado solo pide mockear los datos |
| Volcado SQL precargado | Las fechas quedan vencidas enseguida, y el evaluador solo mira desde el día actual |

## 21. Canales de notificación

**Decisión:** cada fila de `notifications` es una entrega en un canal, con la clave `(alert_id, forecast_date, channel)`. El evaluador crea una fila por canal habilitado, y el dispatcher elige el sender según el canal.

**Por qué:** hoy se notifica por WhatsApp, pero un aviso podría salir también por correo o notificación push. El estado de entrega tiene que ser por canal: si WhatsApp funciona y el correo falla, un único `status` no puede representarlo, y reintentar repetiría el WhatsApp. Es lo que hacen los sistemas consolidados:

- [Laravel](https://laravel.com/framework/docs/notifications) crea un job por cada combinación de destinatario y canal.
- En [Knock](https://docs.knock.app/concepts/messages), un mensaje es una notificación para un destinatario en un canal, con un estado de entrega.
- En [Novu](https://docs.novu.co/platform/concepts/workflows), cada paso de canal produce mensajes con su propio canal y estado.

**Alternativas descartadas:**

| Alternativa | Por qué no |
|-------------|------------|
| Tabla padre `notifications` y tabla hija `notification_deliveries` | Con un canal habilitado no aporta nada, agrega una tabla y un paso de reparto, y el benchmark dejaría de ser representativo. Sigue siendo alcanzable agrupando por alerta y fecha |
| Un solo canal en el modelo, y abstracción solo en el código | Agregar un canal después obligaría a cambiar la clave de deduplicación de la tabla central |

**Interfaz:** cada sender declara su `channel` e implementa `send`. Todos son simulados. La aplicación no arranca si un canal habilitado no tiene sender.

**Direcciones:** se resuelven con columnas de `users`: `phone` para WhatsApp y un `email` opcional. Si falta la dirección de un canal, la fila termina como `failed` con el motivo, y queda visible.

**Se tuvo en cuenta a propósito:** el enunciado solo pide notificar al usuario. Modelar los canales cuesta una columna y evita un cambio caro más adelante, por eso se incorporó. Lo que un producto multicanal real necesitaría está en las simplificaciones.

## 22. Colección para pruebas manuales

**Decisión:** la carpeta `collection/` incluye una colección Postman v2.1 y su entorno, que recorren el flujo completo con validaciones.

**Por qué:** permite que un revisor pruebe la API a mano sin armar pedidos. El formato v2.1 es el único que abren las dos herramientas: Postman de forma nativa y Bruno mediante su [importador](https://docs.usebruno.com/get-started/import-export-data/postman-migration), que traduce los scripts.

**Alternativa descartada:** archivos nativos de Bruno. Postman no los abre, y mantener los dos formatos implicaría sincronizarlos a mano.

**Límite aceptado:** los tokens se copian desde la salida del comando de datos simulados.

## 23. Construcción de consultas: SQLAlchemy 2.0

**Decisión:** todas las consultas de la aplicación se construyen con el lenguaje de expresiones de SQLAlchemy, en su estilo moderno: `select()`, `insert()`, `update()`, `Mapped[]` y `mapped_column()`.

**Por qué:** es la forma habitual de trabajar con el stack que pide el enunciado. Las dos sentencias críticas usan funciones específicas de PostgreSQL (`ON CONFLICT`, `SKIP LOCKED`, CTE con `UPDATE`, `unnest`), y SQLAlchemy las expresa todas. Se construyeron de esta forma y se ejecutaron sobre PostgreSQL.

**Alternativas descartadas:**

| Alternativa | Por qué no |
|-------------|------------|
| SQL crudo con `text()` | Pierde tipos, composición y validación de columnas, y no hacía falta para ninguna sentencia |
| API `session.query()` y estilo 1.x | SQLAlchemy la considera [heredada](https://docs.sqlalchemy.org/en/21/changelog/migration_20.html) |

**Cómo se hace cumplir:** [Ruff](https://docs.astral.sh/ruff/rules/banned-api/) prohíbe importar `text()` y `Query` en el código de la aplicación, un test rechaza las llamadas a `.query(`, y los tests fallan ante cualquier advertencia de obsolescencia. Migraciones y tests pueden usar `text()`.

---

## Cumplimiento del enunciado

| Pedido del enunciado | Cómo se cumple | Estado |
|----------------------|----------------|--------|
| Aprovechar datos meteorológicos ya disponibles | `weather_forecasts`, alimentada por una ingesta simulada | Diseñado |
| Una entrada por día y evento, con su probabilidad | Clave primaria y columna `probability` | Diseñado |
| Definir la tabla meteorológica y mockear sus datos | Tabla definida y datos generados por un comando (decisión 20) | Diseñado |
| Alertas sobre sus campos, con evento y umbral | Tabla `alerts` | Diseñado |
| Un job evalúa periódicamente y genera notificaciones | Evaluador con APScheduler | Diseñado |
| Notificar al usuario | Dispatcher con envío simulado | Diseñado |
| Sin integración con WhatsApp | El envío está detrás de una interfaz simulada | Diseñado |
| Endpoints y API REST | Decisión 19 | Diseñado |
| Código production-ready y asincronía | Decisiones de este documento | En implementación |
| Modelo de datos sólido | [ModeloDeDatos.md](ModeloDeDatos.md) | Diseñado |
| FastAPI, SQLAlchemy, PostgreSQL y Alembic | Se usan. APScheduler se justifica en la decisión 3 | Diseñado |
| Repositorio, README, migraciones y tests | Dependen de la implementación | Pendiente |
| Documentar lo que falte | Sección del README | Previsto |

**Interpretaciones propias, porque el enunciado no las define:**

| Tema | Interpretación |
|------|----------------|
| A qué se asocia cada pronóstico | Al campo, porque las alertas se configuran por campo |
| "A partir del cual" o "se superan" | `>=` |
| Qué significa no repetir una notificación | Una por alerta, fecha y canal |
| Cuántas alertas por campo y evento | Una vigente |

**Lo que va más allá del mínimo:** el enunciado dice que "alcanza con los endpoints, el background job de evaluación y su funcionalidad". La entrega con reservas y reintentos excede esa frase. Se mantiene porque el enunciado también exige notificar al usuario y código production-ready, y una notificación que se pierde por un fallo no cumpliría ninguna de las dos cosas. Es deliberadamente pequeña, y el envío es simulado. Los canales múltiples también exceden lo pedido, y la decisión 21 explica por qué se incorporaron.

---

## Simplificaciones frente a producción

El objetivo es un sistema simple, sin construir todo lo que rodea a un producto real. Cada vez que se eligió lo simple y producción merecería otra cosa, queda registrado aquí.

| Qué se simplificó | Qué haría falta en producción | Por qué alcanza aquí | Referencia |
|-------------------|-------------------------------|----------------------|------------|
| Ingesta simulada | Integración con un proveedor real, normalizando escalas y derivando heladas | El enunciado pide mockear estos datos | Decisión 4 |
| Envío simulado | WhatsApp Cloud API con [plantillas aprobadas](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview), opt-in y manejo del error `130429` | El enunciado excluye la integración | [Throughput](https://developers.facebook.com/documentation/business-messaging/whatsapp/throughput/) |
| Tokens generados por un script | El emisor real de Agrobot | El enunciado no pide login | Decisión 18 |
| Secreto compartido (HS256) | Firma asimétrica y JWKS: la API solo tendría la clave pública y no podría emitir tokens | Hay un solo servicio y ningún emisor real. Se exige un secreto de 256 bits | [Auth0](https://auth0.com/docs/get-started/applications/signing-algorithms) |
| Tokens sin revocación | Lista de revocación o tokens de vida muy corta con renovación | Se limita la exposición con una expiración corta | [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html) |
| Sin límite de pedidos en la API | Límite por cliente, en la aplicación o en el gateway | No hay tráfico real. Sí se limita el tamaño de página | [OWASP API4:2023](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/) |
| Identificadores `bigint` en la API | Identificadores no predecibles | La pertenencia se verifica en cada consulta, que es la protección real | [OWASP API1:2023](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/) |
| Logs estructurados, sin métricas | Métricas y alertas de latencia, tráfico, errores y saturación, además de filas pendientes y fallidas | El ejercicio no incluye una plataforma de monitoreo | [Google SRE](https://sre.google/sre-book/monitoring-distributed-systems/) |
| Sin control de datos desactualizados | Una alerta si `fetched_at` deja de avanzar | El enunciado pide evaluar los datos almacenados, y un aviso con datos viejos suele ser mejor que ninguno | Enunciado |
| `notifications` sin retención | Particionar por fecha y eliminar particiones antiguas | El volumen del ejercicio no lo requiere | [PostgreSQL](https://www.postgresql.org/docs/current/ddl-partitioning.html) |
| Autovacuum sin ajustar | Ajuste por tabla, después de medir carga sostenida | No se midió una carga sostenida, y no se ajusta sin evidencia | Decisión 12 |
| Réplicas sin reparto de trabajo | Repartir rangos de alertas si una corrida deja de entrar en su intervalo | El peor caso medido tarda unos 11 s | Decisión 12 |
| Un mensaje por notificación | Agrupar por usuario y día | El enunciado pide notificar, no agrupar | Decisión 7 |
| Envío a cualquier hora | Respetar franjas horarias según la zona del campo | El enunciado no lo pide | Decisión 10 |
| Pronóstico por campo | Tabla compartida por ubicación | Es el modelo más simple y la ingesta está fuera de alcance | Decisión 5 |
| Un destinatario por campo | Una relación de destinatarios | El enunciado habla de "sus campos" | Decisión 15 |
| Canales habilitados de forma global | Preferencias por usuario, canal y tipo de evento | El enunciado no pide elegir canal | [Diseño de entrega](https://www.techinterview.org/post/3233469741/lld-notification-delivery/) |
| Direcciones como columnas de `users` | Una tabla de puntos de contacto, con verificación y varios dispositivos | Alcanza para los canales simulados | Decisión 21 |
| Un solo grupo de envío para todos los canales | Colas y límites por canal | Los envíos son simulados | [Laravel](https://laravel.com/framework/docs/notifications) |

---

## Casos borde

| Caso | Comportamiento |
|------|----------------|
| La probabilidad es exactamente igual al umbral | Se notifica |
| El job corre otra vez y la condición sigue vigente | No se repite la notificación |
| El pronóstico baja y vuelve a subir | No se repite: la clave es alerta, fecha y canal |
| Dos workers evalúan a la vez | La restricción única evita duplicados |
| El proceso cae después de guardar y antes de enviar | La fila queda pendiente y se envía luego |
| El proceso cae después de enviar y antes de registrarlo | Puede haber un envío duplicado |
| Medianoche en otro país | Cada campo usa su propia fecha local |
| Un proveedor informa en escala 0 a 1 | La ingesta normaliza. Un `CHECK` no detecta un `0.7` que signifique 70 % |
| Un usuario tiene varios campos | Cada alerta genera sus propias notificaciones |
| Primera corrida grande con la tabla de notificaciones vacía | Sin anti join, el plan no depende de sus estadísticas |
| Se intenta guardar un campo con una zona horaria inválida | Se rechaza al escribir, porque haría fallar la evaluación de todo un lote |
| El usuario borra una alerta con notificaciones pendientes | Las ya creadas se entregan y no se crean nuevas |
| Un usuario pide un recurso de otro | Responde `404`, igual que si no existiera |
| El token venció o tiene otro algoritmo | Responde `401` |
| Un canal se entrega y otro falla | Cada uno tiene su propio estado. Solo se reintenta el que falló |
| El usuario no tiene dirección para un canal | Esa fila termina como `failed`, con el motivo |
| Se habilita un canal nuevo | Notifica por ese canal las fechas futuras que sigan superando el umbral |
| El worker está caído hasta que pasa la fecha del pronóstico | Ese día ya no se evalúa, porque solo se consideran fechas futuras o la actual |

## Fuera de alcance

- Integración con WhatsApp, según el enunciado.
- Ingesta real desde un proveedor: los datos se simulan.
- Alta y edición de usuarios y campos, que pertenecen a Agrobot.
- Agrupar varias notificaciones en un solo mensaje.

## Temas abiertos

No quedan temas de diseño abiertos.

## Fuentes

**Librerías (decisión 3)**

- [Celery: tareas periódicas](https://github.com/celery/celery/blob/v5.7.0/docs/userguide/periodic-tasks.rst)
- [Celery: soporte de asyncio, issue 3884](https://github.com/celery/celery/issues/3884)
- [SQLAlchemy: múltiples event loops](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/orm/extensions/asyncio.rst)
- [APScheduler: AsyncIOExecutor](https://apscheduler.readthedocs.io/en/3.x/modules/executors/asyncio.html)
- [APScheduler: README](https://github.com/agronholm/apscheduler/blob/master/README.rst)
- [Procrastinate: tareas periódicas](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cron.html)
- [Procrastinate: migraciones](https://procrastinate.readthedocs.io/en/stable/howto/production/migrations.html)
- [arq: README](https://github.com/samuelcolvin/arq/blob/main/README.md?plain=1)

**Proveedores de clima (decisión 4)**

- [Open-Meteo](https://open-meteo.com/en/docs)
- [OpenWeather One Call 3.0](https://openweathermap.org/api/one-call-3)
- [WeatherAPI](https://www.weatherapi.com/docs/forecast-api/)
- [Tomorrow.io](https://docs.tomorrow.io/reference/data-layers-core)
- [Visual Crossing](https://www.visualcrossing.com/resources/documentation/weather-api/timeline-weather-api/)
- [AccuWeather, documentación de terceros](https://accuweatherapiwrapperdocs.pages.dev/interfaces/IDayNightWeatherSummary)
- [ECMWF: probabilidad de temperatura bajo 0 °C](https://www.ecmwf.int/node/37731)

**Superficies y resolución (decisiones 5 y 6)**

- [CNA 2018, Argentina](https://bichosdecampo.com/cna-2018-de-las-250-mil-explotaciones-el-84-son-manejadas-por-personas-pero-las-sociedades-manejan-38-del-campo)
- [Uruguay](https://cefs.ncsu.edu/wp-content/uploads/produccionagrouruguayparancsuenglish.pdf)
- [México, INEGI](https://www.inegi.org.mx/contenidos/saladeprensa/boletines/2024/CA_Adic/CA_Adic2024.pdf)
- [Paraguay](https://countrystudies.us/paraguay/42.htm)
- [Open-Meteo y ECMWF a 9 km](https://openmeteo.substack.com/p/ecmwf-transitions-to-open-data)
- [Modelos en Open-Meteo](https://openmeteo.substack.com/p/best-weather-models-in-one-open-source)
- [Resolución de GFS e ICON](https://meteoensevilla.duckdns.org/wxguia-modelos.php?lang=en)
- [Variabilidad espacial de heladas](https://ives-openscience.eu/date/2022/03/page/3)

**Entrega y modelo (decisiones 12, 14 y 16)**

- [WhatsApp: throughput](https://developers.facebook.com/documentation/business-messaging/whatsapp/throughput/)
- [Amazon SQS: visibility timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
- [pgmq](https://github.com/pgmq/pgmq)
- [AWS: reintentos y backoff con jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
- [PostgreSQL: tipos enum](https://www.postgresql.org/docs/current/datatype-enum.html)
- [FieldData: oferta laboral](https://arc.dev/remote-jobs/j/fielddata-software-engineer-p8tz8qg3mg)
