# Procedimiento

Este documento cuenta cómo encaré el challenge: en qué orden trabajé, qué reglas me puse y qué cambió en el camino. El detalle técnico de cada elección está en [Decisiones.md](Decisiones.md).

## En resumen

1. Leí el enunciado buscando qué se evalúa realmente.
2. Investigué antes de decidir, con fuentes primarias y mediciones.
3. Discutí el alcance y el modelo de datos antes de escribir código.
4. Seguí el orden PRD, especificación, plan e implementación.
5. Cerré con tests, mediciones sobre el código final y documentación de lo que faltó.

## Cómo leí el enunciado

El enunciado pide tres cosas: código production-ready, un modelo de datos sólido y asincronía. También deja el diseño a criterio del candidato, así que entendí que la evaluación está en las decisiones y en cómo se justifican.

Antes de diseñar, listé los problemas que aparecen en cuanto el sistema corre de verdad:

- Un job periódico vuelve a encontrar la misma condición en cada corrida.
- La ingesta puede cambiar un pronóstico después de haber notificado.
- Puede haber más de un worker al mismo tiempo.
- El proceso puede caerse entre guardar una notificación y enviarla.
- "Hoy" no es el mismo día en todos los países donde opera la empresa.
- El enunciado usa dos criterios distintos para el umbral: "a partir del cual" y "se superan".

## Reglas que me puse

| Regla | Qué significa en la práctica |
|-------|------------------------------|
| Fidelidad al enunciado | No agregar funciones que no pide. Si algo lo excede, se justifica o queda afuera |
| Lo probado antes que lo novedoso | Elegir por evidencia de uso y mantenimiento, no por preferencia |
| Investigar antes de afirmar | Verificar en documentación oficial o medir, sin responder de memoria |
| Simple, sin atajos | Sin soluciones improvisadas y sin abstracciones que no resuelvan un problema real |
| Foco en las notificaciones | Los datos simulados se mantienen simples |
| Documentar el porqué | Registrar alternativas descartadas, límites y casos borde, con fuentes |
| Simplificar con criterio | Si lo simple no es lo que merecería producción, dejarlo escrito y justificado |

## Uso de inteligencia artificial

Trabajé con Claude Code como herramienta. Yo definí el alcance, las prioridades y las decisiones finales, y cuestioné las propuestas. La herramienta investigó, comparó opciones, corrió mediciones y redactó borradores que luego revisé.

## Cómo fue avanzando

| Etapa | Qué hice | Qué resultó |
|-------|----------|-------------|
| Contexto | Leí el enunciado, la oferta laboral y notas de prensa sobre FieldData | Una empresa que valora la simplicidad y un equipo chico: conviene código fácil de revisar |
| Forma del sistema | Comparé un módulo, un servicio sobre la misma base y un microservicio | Módulo con procesos `api` y `worker`. Como Agrobot es hipotético, se simula |
| Librería del job | Audité diez librerías: licencias del árbol completo, adopción, mantenimiento y soporte async | APScheduler 3.x. Celery es la más probada, pero no se ajusta a un stack async sin rodeos |
| Modelo de datos | Revisé qué devuelven los proveedores reales de clima | Una fila por campo, día y evento. Ningún proveedor consultado informa probabilidad de helada, así que se deriva |
| Volumen | Me pregunté qué pasa si los campos se duplican en un año y un solo job genera muchas notificaciones | Medí a 24.000 campos: evaluar en SQL mantiene la memoria plana y conviene trabajar por lotes |
| Granularidad | Cuestioné si tenía sentido alertar por lote | No: una celda de pronóstico suele ser mucho más grande que un campo |
| Canales | Me pregunté si una notificación podía salir por varios canales | Sí. El estado de entrega pasó a ser por canal, con una sola columna más |

## Correcciones en el camino

**Un `NOT EXISTS` que parecía inofensivo.** Lo agregué a la evaluación para evitar trabajo innecesario. En el benchmark, una variante con 4 evaluadores tardó más de 27 minutos en lugar de 16 segundos. La tabla se había analizado vacía, y PostgreSQL eligió un plan que la recorría por completo para cada candidato. Sin esa condición, la unicidad sigue garantizada y el problema desaparece. Sin medir, no lo habría encontrado.

**Campos que comparten celda.** Al principio di por hecho que pocos campos compartirían una celda del modelo meteorológico. Al calcular superficies para evaluar la granularidad por lote, los números mostraron otra cosa: en una celda de 9 km entran unos 13 campos promedio de Argentina.

La decisión de asociar los pronósticos a `field_id` se mantuvo, pero cambió su justificación. Ya no se apoya en que compartir celda sea raro, sino en la simplicidad, en la fidelidad al enunciado y en que la ingesta está fuera de alcance.

**Una reserva que podía superar el tamaño del lote.** Con estadísticas desactualizadas, PostgreSQL podía volver a ejecutar la subconsulta de la reserva y tomar más filas de las pedidas. Lo reproduje en PostgreSQL 17 y pasé la selección a una CTE `MATERIALIZED`, que se evalúa una sola vez.

**Una CTE que acotaba el lote pero recorría la tabla.** Cuestioné si la CTE `MATERIALIZED` de la reserva hacía falta. Al medirla, vi que mantenía el lote acotado, pero recorría la tabla entera en cada reserva: con 100.000 filas pendientes, unos 23 ms. Una sola sentencia con `ARRAY(...)` también respeta el lote y actualiza por clave primaria: unos 7 ms. La comparación se repite con `scripts/claim_forms.sql` (decisión 14). Pasé la reserva a esa sentencia.

**La medición final no repitió un número.** Volví a medir con el código real. La evaluación coincidió con lo esperado, pero la entrega tardó 917 s en lugar de 34 a 40 s. Después de cambiar la reserva volví a medir, esta vez separando los dos pasos: 8,71 s de evaluación y 863 s de entrega, de los que 24,0 s fueron la reserva y 836,9 s el registro de resultados. El benchmark inicial registraba cada lote con un solo `UPDATE`, y el código final lo hace fila por fila. La velocidad sigue siendo muy superior al límite del proveedor, así que lo dejé como está y lo registré en [Decisiones.md](Decisiones.md#12-volumen-y-batching).

**Lo que encontró la integración continua.** La primera ejecución local encontró 7 vulnerabilidades HIGH con corrección en la imagen base, ninguna en las dependencias de la aplicación: zlib1g, y msgpack, setuptools y urllib3 incluidos dentro de pip. Actualicé los paquetes de Debian en la imagen y eliminé pip. La revisión de seguridad encontró además que el escaneo de ZAP, tal como estaba escrito, nunca podía fallar: usaba `-I` y no tenía archivo de reglas. Quité `-I`, agregué `.zap/rules.tsv` y comprobé ambos casos: sin el archivo, el escaneo falla por las dos alertas WARN; con él, esas dos se ignoran y cualquier otra alerta hace fallar el trabajo.

## Estado final

- [x] Cerrar volumen y batching con mediciones.
- [x] Definir las tablas `alerts` y `notifications`, y verificar el esquema en PostgreSQL.
- [x] Definir la API y la autenticación.
- [x] Definir el generador de datos simulados.
- [x] Escribir el PRD.
- [x] Escribir la especificación.
- [x] Escribir el plan de implementación.
- [x] Implementar con tests sobre PostgreSQL real.
- [x] Volver a medir con las sentencias finales y auditar las licencias de todas las dependencias.
- [x] Documentar lo que faltó, en el [README](../README.md#lo-que-faltó).
