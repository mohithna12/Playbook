"""Extensions, enum types, and the uuidv7() function.

Revision ID: 0001_extensions
Revises:
Create Date: 2026-08-05

Postgres 16 has no built-in ``uuidv7()`` (it lands in 18) and the ``pg_uuidv7``
extension is not available on RDS. Rather than take a dependency the managed
service cannot satisfy, this defines uuidv7() in plpgsql: a time-ordered UUID
whose first 48 bits are a millisecond timestamp. Time ordering matters because
these are primary keys of insert-heavy tables — random UUIDv4 keys fragment
B-tree pages and inflate WAL. Generation stays server-side so a raw SQL insert,
Alembic, and the ORM all produce the same key shape.

Every reference is schema-qualified as ``public.uuidv7()``. Postgres 18 ships a
built-in ``pg_catalog.uuidv7()``, and pg_catalog is searched ahead of public
regardless of ``search_path`` — so an unqualified name silently binds to the
built-in on 18 and to this function on 16. Qualifying keeps primary-key
generation identical across server versions, and keeps the downgrade from
trying to drop a system function.
"""

from __future__ import annotations

from alembic import op

revision = "0001_extensions"
down_revision = None
branch_labels = None
depends_on = None

UUIDV7_FN = """
CREATE OR REPLACE FUNCTION public.uuidv7() RETURNS uuid
AS $$
DECLARE
    unix_ts_ms  bytea;
    uuid_bytes  bytea;
BEGIN
    -- 48-bit big-endian millisecond timestamp
    unix_ts_ms := substring(
        int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3
    );
    uuid_bytes := uuid_send(gen_random_uuid());
    uuid_bytes := overlay(uuid_bytes PLACING unix_ts_ms FROM 1 FOR 6);
    -- version 7 in the high nibble of byte 6
    uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15) | 112);
    -- RFC 4122 variant (10xx) in the high bits of byte 8
    uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
    RETURN encode(uuid_bytes, 'hex')::uuid;
END
$$ LANGUAGE plpgsql VOLATILE;
"""

ENUMS: dict[str, tuple[str, ...]] = {
    "league_provider": ("SLEEPER", "ESPN", "YAHOO"),
    "sync_status": ("PENDING", "SYNCING", "ACTIVE", "PARTIAL", "FAILED"),
    "player_position": ("QB", "RB", "WR", "TE", "K", "DST", "OL", "DL", "LB", "DB"),
    "recommendation_kind": ("LINEUP", "TRADE", "WAIVER", "START_SIT"),
    "job_status": (
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "PARTIAL",
        "CANCELLED",
    ),
}


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    # citext: case-insensitive email uniqueness without a functional index.
    # pg_trgm: fuzzy player-name search and the identity-resolution fallback.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(UUIDV7_FN)

    for name, values in ENUMS.items():
        rendered = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    for name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")

    op.execute("DROP FUNCTION IF EXISTS public.uuidv7()")
    # Extensions are intentionally left in place: they are cheap, may be shared
    # with other schemas on the instance, and dropping citext would cascade
    # into any column still using it.
