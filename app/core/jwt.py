from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import ALGORITHM, AUTH_COOKIE_NAME, JWT_SECRET_KEY
from app.core.security import access_token_expire_time
from app.db.session import get_db
from app.models.user import User

# Swagger 인증용. 실제 프론트 인증은 HttpOnly Cookie를 우선 사용한다.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/swagger-login", auto_error=False)


def create_access_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": access_token_expire_time(),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)


def get_token_from_request(request: Request, bearer_token: str | None) -> str | None:
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        return cookie_token

    # 기존 Authorization: Bearer 방식도 Swagger/호환성을 위해 유지한다.
    return bearer_token


def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증이 필요합니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    request_token = get_token_from_request(request, token)
    if not request_token:
        raise credentials_exception

    try:
        payload = jwt.decode(request_token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비활성화된 계정입니다. 관리자에게 문의하세요.",
        )

    return user
