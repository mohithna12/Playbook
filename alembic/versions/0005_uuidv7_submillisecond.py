"""Give public.uuidv7() sub-millisecond ordering.

Revision ID: 0005_uuidv7_subms
Revises: 0004_application
Create Date: 2026-08-07

The 0001 definition put a 48-bit millisecond timestamp in the high bytes and
left the rest random, so two rows inserted within the same millisecond -- the
common case for a batch insert or a fast request -- came out in random order.
That is enough for the B-tree locality 0001 was after, but it breaks the
stronger property the schema tests assert and that anything paging by primary
key relies on: sequential inserts produce increasing keys.

This replaces the function with RFC 9562's "replace leftmost random bits with
increased clock precision" method: the 12 ``rand_a`` bits after the version
nibble carry the sub-millisecond fraction, taking the resolution from 1 ms to
about 244 ns. The remaining 62 bits stay random, so the collision properties
are unchanged.

Written as a new migration rather than an edit to 0001 because 0001 is already
applied wherever this schema has been deployed; ``CREATE OR REPLACE`` makes the
upgrade a metadata-only change with no table rewrite.
"""

from __future__ import annotations

from alembic import op

revision = "0005_uuidv7_subms"
down_revision = "0004_application"
branch_labels = None
depends_on = None

# `clock_timestamp()` is deliberate, not `now()`: now() is fixed for the whole
# transaction, so a multi-row insert would otherwise share one timestamp and
# land back in random order.
UUIDV7_SUBMS = """
CREATE OR REPLACE FUNCTION public.uuidv7() RETURNS uuid
AS $$
DECLARE
    epoch_ms    numeric;
    unix_ts_ms  bytea;
    sub_ms      int;
    uuid_bytes  bytea;
BEGIN
    epoch_ms := extract(epoch FROM clock_timestamp()) * 1000;

    -- 48-bit big-endian millisecond timestamp
    unix_ts_ms := substring(int8send(floor(epoch_ms)::bigint) FROM 3);

    -- Fractional millisecond scaled into the 12 rand_a bits (0..4095).
    sub_ms := floor((epoch_ms - floor(epoch_ms)) * 4096)::int;

    uuid_bytes := uuid_send(gen_random_uuid());
    uuid_bytes := overlay(uuid_bytes PLACING unix_ts_ms FROM 1 FOR 6);
    -- byte 6: version 7 in the high nibble, rand_a bits 11..8 in the low
    uuid_bytes := set_byte(uuid_bytes, 6, 112 | ((sub_ms >> 8) & 15));
    -- byte 7: rand_a bits 7..0
    uuid_bytes := set_byte(uuid_bytes, 7, sub_ms & 255);
    -- RFC 4122 variant (10xx) in the high bits of byte 8
    uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
    RETURN encode(uuid_bytes, 'hex')::uuid;
END
$$ LANGUAGE plpgsql VOLATILE;
"""

UUIDV7_MILLISECOND = """
CREATE OR REPLACE FUNCTION public.uuidv7() RETURNS uuid
AS $$
DECLARE
    unix_ts_ms  bytea;
    uuid_bytes  bytea;
BEGIN
    unix_ts_ms := substring(
        int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3
    );
    uuid_bytes := uuid_send(gen_random_uuid());
    uuid_bytes := overlay(uuid_bytes PLACING unix_ts_ms FROM 1 FOR 6);
    uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15) | 112);
    uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
    RETURN encode(uuid_bytes, 'hex')::uuid;
END
$$ LANGUAGE plpgsql VOLATILE;
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(UUIDV7_SUBMS)


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(UUIDV7_MILLISECOND)
