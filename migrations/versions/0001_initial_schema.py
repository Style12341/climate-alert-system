"""Initial schema: six tables, every rule from ModeloDeDatos.md, event catalog.

Written by hand because autogenerate sees neither CHECK constraints nor changes
to the WHERE of a partial index.

Every constraint name goes through op.f(). Alembic applies the naming convention
of target_metadata here too, so a bare "ck_..." would be prefixed a second time.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
PERCENTAGE = sa.Numeric(5, 2)
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("phone", name=op.f("uq_users_phone")),
    )

    op.create_table(
        "fields",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fields")),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_fields_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(TIMESTAMP '2000-01-01 00:00' AT TIME ZONE timezone) IS NOT NULL",
            name=op.f("ck_fields_timezone_valid"),
        ),
    )
    op.create_index(op.f("ix_fields_owner_id"), "fields", ["owner_id"])

    event_types = op.create_table(
        "climate_event_types",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_climate_event_types")),
    )
    op.bulk_insert(
        event_types,
        [
            {"code": "frost", "label": "Helada"},
            {"code": "rain", "label": "Lluvia"},
            {"code": "hail", "label": "Granizo"},
            {"code": "snow", "label": "Nieve"},
            {"code": "thunderstorm", "label": "Tormenta"},
            {"code": "heat_wave", "label": "Ola de calor"},
            {"code": "strong_wind", "label": "Viento fuerte"},
        ],
    )

    op.create_table(
        "weather_forecasts",
        sa.Column("field_id", sa.BigInteger(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("probability", PERCENTAGE, nullable=False),
        sa.Column("fetched_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint(
            "field_id",
            "forecast_date",
            "event_type",
            name=op.f("pk_weather_forecasts"),
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["fields.id"],
            name=op.f("fk_weather_forecasts_field_id_fields"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_type"],
            ["climate_event_types.code"],
            name=op.f("fk_weather_forecasts_event_type_climate_event_types"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "probability BETWEEN 0 AND 100",
            name=op.f("ck_weather_forecasts_probability_range"),
        ),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("field_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("threshold", PERCENTAGE, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.Column("deleted_at", TIMESTAMPTZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["fields.id"],
            name=op.f("fk_alerts_field_id_fields"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_type"],
            ["climate_event_types.code"],
            name=op.f("fk_alerts_event_type_climate_event_types"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "threshold > 0 AND threshold <= 100",
            name=op.f("ck_alerts_threshold_range"),
        ),
    )
    op.create_index(
        op.f("uq_alerts_live_field_event"),
        "alerts",
        ["field_id", "event_type"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("probability", PERCENTAGE, nullable=False),
        sa.Column("threshold", PERCENTAGE, nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.Column("sent_at", TIMESTAMPTZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_notifications_alert_id_alerts"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "alert_id",
            "forecast_date",
            "channel",
            name=op.f("uq_notifications_alert_date_channel"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name=op.f("ck_notifications_status_valid"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_notifications_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)",
            name=op.f("ck_notifications_sent_at"),
        ),
    )
    op.create_index(
        op.f("ix_notifications_due"),
        "notifications",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("notifications")
    op.drop_table("alerts")
    op.drop_table("weather_forecasts")
    op.drop_table("climate_event_types")
    op.drop_table("fields")
    op.drop_table("users")
