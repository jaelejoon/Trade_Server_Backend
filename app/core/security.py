from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from passlib.context import CryptContext

from app.core.config import ACCESS_TOKEN_EXPIRE_MINUTES, ENCRYPTION_KEY

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
fernet = Fernet(ENCRYPTION_KEY.encode())


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(raw_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(raw_password, hashed_password)


def encrypt_text(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_text(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()


def access_token_expire_time() -> datetime:
    return now_utc() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
