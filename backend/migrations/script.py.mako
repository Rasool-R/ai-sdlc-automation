"""Alembic migration script template."""

${basemeta}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades}


def downgrade() -> None:
    ${downgrades}