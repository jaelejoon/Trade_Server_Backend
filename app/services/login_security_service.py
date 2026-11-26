from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import (
    LOGIN_FAILURE_LIMIT,
    LOGIN_FAILURE_WINDOW_MINUTES,
    LOGIN_LOCK_MINUTES,
)
from app.core.security import now_utc
from app.models.login_attempt import LoginAttempt

GENERIC_LOGIN_ERROR = '이메일 또는 비밀번호가 틀렸습니다.'
LOCKED_LOGIN_ERROR = '로그인 실패 횟수가 많아 일시적으로 잠겼습니다. 잠시 후 다시 시도하세요.'


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_login_attempt(db: Session, email: str, ip_address: str) -> LoginAttempt:
    normalized_email = normalize_email(email)
    attempt = (
        db.query(LoginAttempt)
        .filter(
            LoginAttempt.email == normalized_email,
            LoginAttempt.ip_address == ip_address,
        )
        .first()
    )

    if attempt:
        return attempt

    attempt = LoginAttempt(
        email=normalized_email,
        ip_address=ip_address,
        failed_count=0,
        locked_until=None,
        first_failed_at=None,
        last_failed_at=None,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def assert_login_not_locked(db: Session, email: str, ip_address: str) -> None:
    attempt = get_login_attempt(db, email, ip_address)
    now = now_utc()

    if attempt.locked_until and attempt.locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=LOCKED_LOGIN_ERROR,
        )

    if attempt.locked_until and attempt.locked_until <= now:
        attempt.failed_count = 0
        attempt.locked_until = None
        attempt.first_failed_at = None
        attempt.last_failed_at = None
        db.commit()


def record_login_failure(db: Session, email: str, ip_address: str) -> None:
    attempt = get_login_attempt(db, email, ip_address)
    now = now_utc()

    window_start = now - timedelta(minutes=LOGIN_FAILURE_WINDOW_MINUTES)
    if attempt.first_failed_at is None or attempt.first_failed_at < window_start:
        attempt.failed_count = 1
        attempt.first_failed_at = now
    else:
        attempt.failed_count += 1

    attempt.last_failed_at = now

    if attempt.failed_count >= LOGIN_FAILURE_LIMIT:
        attempt.locked_until = now + timedelta(minutes=LOGIN_LOCK_MINUTES)

    db.commit()


def record_login_success(db: Session, email: str, ip_address: str) -> None:
    attempt = get_login_attempt(db, email, ip_address)
    attempt.failed_count = 0
    attempt.locked_until = None
    attempt.first_failed_at = None
    attempt.last_failed_at = None
    db.commit()


def raise_invalid_login() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=GENERIC_LOGIN_ERROR,
    )
