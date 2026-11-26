import hashlib
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import REFRESH_TOKEN_EXPIRE_DAYS
from app.core.security import now_utc
from app.models.refresh_token import RefreshToken
from app.models.user import User


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(db: Session, user: User) -> str:
    raw_token = secrets.token_urlsafe(64)
    token = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_token),
        expires_at=now_utc() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(token)
    db.commit()
    return raw_token


def get_valid_refresh_token(db: Session, raw_token: str) -> RefreshToken | None:
    token_hash = hash_refresh_token(raw_token)
    token = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()

    if not token:
        return None

    current_time = now_utc()
    if token.revoked_at is not None:
        return None

    if token.expires_at <= current_time:
        return None

    return token


def revoke_refresh_token(db: Session, token: RefreshToken) -> None:
    if token.revoked_at is None:
        token.revoked_at = now_utc()
        db.add(token)
        db.commit()


def revoke_all_user_refresh_tokens(db: Session, user_id: int) -> None:
    current_time = now_utc()
    tokens = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user_id)
        .filter(RefreshToken.revoked_at.is_(None))
        .all()
    )

    for token in tokens:
        token.revoked_at = current_time
        db.add(token)

    db.commit()
