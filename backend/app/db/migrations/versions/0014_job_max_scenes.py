"""jobs.max_scenes

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-27

A quick-fetch job (the dashboard's "Fetch satellite data" button, or the
Telegram bot's /status-triggered scan) deliberately stops after N usable
scenes rather than processing every usable scene in its window. job_view's
"done" heuristic didn't know that, so a capped job sat at status="running"
forever even after its Celery task succeeded -- this column lets it tell the
two cases apart.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("max_scenes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "max_scenes")
