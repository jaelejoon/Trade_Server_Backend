from sqlalchemy import create_engine

from app.core.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    hide_parameters=True,
    pool_size=10,
    max_overflow=20,
)
