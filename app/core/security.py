from datetime import datetime, timedelta, timezone
from typing import Any
import re

from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext

from app.core.config import ACCESS_TOKEN_EXPIRE_MINUTES, ENCRYPTION_KEY

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
fernet = Fernet(ENCRYPTION_KEY.encode())


SENSITIVE_REPLACEMENTS = [
    (re.compile(r"(signature=)[^&\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(X-MBX-APIKEY['\"\s:=]+)[^,'\"\s}]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(api[_-]?key['\"\s:=]+)[^,'\"\s}]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(secret[_-]?key['\"\s:=]+)[^,'\"\s}]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Authorization['\"\s:=]+Bearer\s+)[^,'\"\s}]+", re.IGNORECASE), r"\1***"),
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(raw_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(raw_password, hashed_password)


def encrypt_text(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_text(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()


def try_decrypt_text(value: str) -> str | None:
    try:
        return decrypt_text(value)
    except InvalidToken:
        return None


def mask_sensitive_text(value: str | None, keep_start: int = 6, keep_end: int = 4) -> str:
    if not value:
        return ""

    if len(value) <= keep_start + keep_end:
        return "*" * len(value)

    return f"{value[:keep_start]}{'*' * 8}{value[-keep_end:]}"


def sanitize_sensitive_text(value: Any) -> str:
    text = str(value)

    for pattern, replacement in SENSITIVE_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    return text


def ensure_encrypted_value(encrypted_value: str, plain_value: str, field_name: str) -> None:
    if encrypted_value == plain_value:
        raise ValueError(f"{field_name} 값이 암호화되지 않은 상태입니다.")

    decrypted = try_decrypt_text(encrypted_value)
    if decrypted != plain_value:
        raise ValueError(f"{field_name} 암호화 검증에 실패했습니다.")


def access_token_expire_time() -> datetime:
    return now_utc() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)