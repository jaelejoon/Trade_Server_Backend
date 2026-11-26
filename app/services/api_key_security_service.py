from fastapi import HTTPException

from app.core.security import encrypt_text, ensure_encrypted_value


def encrypt_api_credentials(api_key: str, secret_key: str) -> tuple[str, str]:
    api_key_encrypted = encrypt_text(api_key)
    secret_key_encrypted = encrypt_text(secret_key)

    try:
        ensure_encrypted_value(api_key_encrypted, api_key, "api_key")
        ensure_encrypted_value(secret_key_encrypted, secret_key, "secret_key")
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="API Key 암호화 검증에 실패했습니다.") from exc

    return api_key_encrypted, secret_key_encrypted
