"""Add internal_phone_numbers registry + seed with 25 RC-pulled rows.

Backstory: caller `0353030263` was rendering as a customer ("Bill
Parker") on the Customer 360 page with 2,845 lifetime calls and 8
matching Neto records. The number is actually JJ's primary IVR DID —
the contact-centre Customer Service IB line. The "calls" were every
inbound + outbound the contact centre has ever handled, all stamped
with this DID as the "phone".

Same pattern applies to 17 staff DirectNumbers: when an agent dials a
customer, RC stamps the staff DID as `from_phone_number`, and the
call-history pipeline keys by that field. Result: every customer
Belinda has ever called shows Belinda's DID in their call log instead
of their own number.

Fix: register all 25 RC-configured JJ numbers and short-circuit them
in Customer 360 + filter them out of `call_history_360`. List pulled
live from `account/~/phone-number` + `account/~/extension` on
2026-05-06; refresh via `scripts/sync_rc_internal_numbers.py` when
staff change.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-05-06 09:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f4a5b6c7d8e9'
down_revision = 'e3f4a5b6c7d8'
branch_labels = None
depends_on = None


# Seeded from RC API on 2026-05-06. Source: docs/internal-phone-numbers.md.
SEED_ROWS = [
    # (au_local,    e164,            usage_type,            label,                                ext)
    ("0353030263", "+61353030263", "ContactCenterNumber",  "Customer Service IB (primary IVR)",  None),
    ("0353030264", "+61353030264", "ContactCenterNumber",  "Customer Service OB (outbound IVR)", None),
    ("0353363519", "+61353363519", "CompanyNumber",        None,                                  None),
    ("0353941020", "+61353941020", "ContactCenterNumber",  None,                                  None),
    ("0370430758", "+61370430758", "DirectNumber",         "Ballarat Manager",                    "2001"),
    ("0370434617", "+61370434617", "DirectNumber",         "Warrack Office",                      "3002"),
    ("0370443314", "+61370443314", "DirectNumber",         "Charlie Johnson",                     "1002"),
    ("0370443330", "+61370443330", "DirectNumber",         "Grant Jonasson",                      "1003"),
    ("0370443359", "+61370443359", "DirectNumber",         "Belinda Battistin",                   "1004"),
    ("0370443362", "+61370443362", "DirectNumber",         "Tamara Webster",                      "1005"),
    ("0370443461", "+61370443461", "DirectNumber",         "Dallas Redenbach",                    "1006"),
    ("0370443527", "+61370443527", "ContactCenterNumber",  None,                                  None),
    ("0370443574", "+61370443574", "DirectNumber",         "Kate Vandeheuvel",                    "1008"),
    ("0370443577", "+61370443577", "DirectNumber",         "Ballarat Stock desk",                 "2003"),
    ("0370443582", "+61370443582", "DirectNumber",         "Ballarat Register 1",                 "2004"),
    ("0370443583", "+61370443583", "DirectNumber",         "Ballarat Register 3",                 "2005"),
    ("0370443638", "+61370443638", "DirectNumber",         "Mishalee Stulpinas",                  "2006"),
    ("0370443642", "+61370443642", "DirectNumber",         "Dave Malpas",                         "2007"),
    ("0370443665", "+61370443665", "DirectNumber",         "Warrack Counter",                     "3003"),
    ("0370443677", "+61370443677", "DirectNumber",         "Fabio Caris",                         "1009"),
    ("0370450415", "+61370450415", "DirectNumber",         "Shane Dunn",                          "1011"),
    ("0370646824", "+61370646824", "CompanyFaxNumber",     None,                                  None),
    ("0370652670", "+61370652670", "MainCompanyNumber",    "Main company number",                 None),
    ("0370688598", "+61370688598", "DirectNumber",         "Lila Nelis",                          "1012"),
    ("0370762712", "+61370762712", "DirectNumber",         "Donna Tucker",                        "4010"),
]


def upgrade():
    op.create_table(
        'internal_phone_numbers',
        sa.Column('phone',            sa.String(length=20), primary_key=True),
        sa.Column('e164',             sa.String(length=20), nullable=False),
        sa.Column('usage_type',       sa.String(length=40)),
        sa.Column('label',            sa.String(length=120)),
        sa.Column('extension_number', sa.String(length=20)),
        sa.Column('synced_at',        sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
    )
    op.create_index('ix_internal_phone_numbers_e164',
                    'internal_phone_numbers', ['e164'])

    # Seed the 25 known JJ numbers in the same migration so the table is
    # immediately useful — no separate seeding step or "did you remember
    # to run sync_rc?" gotcha for fresh deployments.
    table = sa.table(
        'internal_phone_numbers',
        sa.column('phone'), sa.column('e164'),
        sa.column('usage_type'), sa.column('label'),
        sa.column('extension_number'),
    )
    op.bulk_insert(table, [
        {"phone": p, "e164": e, "usage_type": u, "label": l, "extension_number": ext}
        for p, e, u, l, ext in SEED_ROWS
    ])


def downgrade():
    op.drop_index('ix_internal_phone_numbers_e164',
                  table_name='internal_phone_numbers')
    op.drop_table('internal_phone_numbers')
