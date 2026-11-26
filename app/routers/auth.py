from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH_COOKIE_NAME,
    AUTH_COOKIE_SAMESITE,
    AUTH_COOKIE_SECURE,
    REFRESH_COOKIE_NAME,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from app.core.jwt import create_access_token, get_current_user
from app.core.middleware import get_client_ip
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.services.login_security_service import (
    assert_login_not_locked,
    record_login_failure,
    record_login_success,
    raise_invalid_login,
)
from app.services.refresh_token_service import (
    create_refresh_token,
    get_valid_refresh_token,
    revoke_refresh_token,
)

router = APIRouter(tags=["auth"])


def authenticate_user(db: Session, email: str, password: str, ip_address: str) -> User:
    assert_login_not_locked(db, email, ip_address)

    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        record_login_failure(db, email, ip_address)
        raise_invalid_login()

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비활성화된 계정입니다. 관리자에게 문의하세요.",
        )

    record_login_success(db, email, ip_address)
    return user


def set_access_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=access_token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite=AUTH_COOKIE_SAMESITE,
        path="/",
    )


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite=AUTH_COOKIE_SAMESITE,
        path="/auth",
    )


def clear_access_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        secure=AUTH_COOKIE_SECURE,
        samesite=AUTH_COOKIE_SAMESITE,
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path="/auth",
        secure=AUTH_COOKIE_SECURE,
        samesite=AUTH_COOKIE_SAMESITE,
    )


def issue_cookie_tokens(response: Response, db: Session, user: User) -> str:
    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(db, user)

    set_access_cookie(response, access_token)
    set_refresh_cookie(response, refresh_token)

    return access_token


@router.post("/auth/register", response_model=TokenResponse)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise_invalid_login()

    is_first_user = db.query(User).count() == 0

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        dry_run=body.dry_run,
        is_admin=is_first_user,
        is_prime_admin=is_first_user,
        is_active=True,
        enable_live_trading=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    ip_address = get_client_ip(request)
    record_login_success(db, body.email, ip_address)

    access_token = issue_cookie_tokens(response, db, user)

    # 기존 Swagger/호환성을 위해 access token 응답은 유지한다.
    # 프론트는 localStorage에 저장하지 않고 HttpOnly Cookie를 사용한다.
    return TokenResponse(access_token=access_token)


@router.post("/auth/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    ip_address = get_client_ip(request)
    user = authenticate_user(db, body.email, body.password, ip_address)
    access_token = issue_cookie_tokens(response, db, user)

    # 기존 Swagger/호환성을 위해 access token 응답은 유지한다.
    # 프론트는 localStorage에 저장하지 않고 HttpOnly Cookie를 사용한다.
    return TokenResponse(access_token=access_token)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh_access_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token이 없습니다.",
        )

    refresh_token = get_valid_refresh_token(db, raw_refresh_token)
    if not refresh_token:
        clear_access_cookie(response)
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token이 유효하지 않습니다.",
        )

    user = db.query(User).filter(User.id == refresh_token.user_id).first()
    if not user:
        revoke_refresh_token(db, refresh_token)
        clear_access_cookie(response)
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    if not user.is_active:
        revoke_refresh_token(db, refresh_token)
        clear_access_cookie(response)
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비활성화된 계정입니다. 관리자에게 문의하세요.",
        )

    # Refresh Token Rotation:
    # 사용된 refresh token은 즉시 폐기하고 새 refresh token을 발급한다.
    revoke_refresh_token(db, refresh_token)
    access_token = issue_cookie_tokens(response, db, user)

    return TokenResponse(access_token=access_token)


@router.post("/auth/swagger-login", response_model=TokenResponse)
def swagger_login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    ip_address = get_client_ip(request)
    user = authenticate_user(db, form_data.username, form_data.password, ip_address)

    # Swagger Authorize 버튼은 Bearer 토큰으로 테스트한다.
    return TokenResponse(access_token=create_access_token(user.id, user.email))


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh_token:
        refresh_token = get_valid_refresh_token(db, raw_refresh_token)
        if refresh_token:
            revoke_refresh_token(db, refresh_token)

    clear_access_cookie(response)
    clear_refresh_cookie(response)
    return {"status": "ok"}


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user
