# Challenge: Sistema de Alertas Climáticas

## Contexto

Agrobot es un sistema de gestión agrícola con interfaz de WhatsApp, construido con Python, FastAPI y PostgreSQL.

El sistema ya cuenta con un job de ingesta meteorológica que consulta periódicamente un servicio externo y persiste datos climáticos (probabilidad de lluvias, etc.) en la base de datos.

Podés asumir que para cada día en el futuro hay una entrada por cada evento climático y su probabilidad.

## Objetivo

Implementar un Sistema de Alertas Climáticas que aproveche esos datos ya disponibles.

Los usuarios configuran alertas sobre sus campos, indicando el tipo de evento climático (helada, lluvia, etc.) y un umbral a partir del cual desean ser notificados.

Un background job evalúa periódicamente los datos meteorológicos almacenados contra los umbrales configurados y genera notificaciones cuando se superan.

Cuando una alerta supera el umbral, el sistema debe notificar al usuario.

No hace falta implementar ninguna integración con WhatsApp — alcanza con los endpoints, el background job de evaluación y su funcionalidad.

## Qué buscamos

- Código production-ready
- Modelo de datos sólido
- Asincronía

El diseño del esquema, la API REST y la arquitectura quedan a discreción del candidato. Esto incluye definir la tabla de datos meteorológicos que el job de ingesta ya alimenta (estos datos deben ser mockeados a los efectos del ejercicio).

## Stack

FastAPI + SQLAlchemy + PostgreSQL + Alembic (preferido). Se pueden usar otras herramientas si se justifica la elección.

## Entregable

- Repositorio con el código.
- README que explique cómo correrlo y las decisiones tomadas.
- Migraciones funcionales.
- Tests.

Si por alguna razón no alcanzara el tiempo para completar todo, se puede documentar lo que faltó.
