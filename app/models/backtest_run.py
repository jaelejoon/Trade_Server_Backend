from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey
from sqlalchemy import Identity, Index, Integer, Table, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


backtest_runs = Table(
    "backtest_runs",
    Base.metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("status", Text, nullable=False),
    Column("request", JSONB, nullable=False),
    Column("result", JSONB),
    Column("error_message", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')"),
)

Index("backtest_runs_user_history_idx", backtest_runs.c.user_id, backtest_runs.c.id.desc())
Index(
    "backtest_runs_active_idx",
    backtest_runs.c.status,
    backtest_runs.c.updated_at,
    postgresql_where=text("status IN ('queued', 'running')"),
)
