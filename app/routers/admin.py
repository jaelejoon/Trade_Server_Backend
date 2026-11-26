from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.trade_log import TradeLog
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.scheduler.bot_scheduler import stop_account_job
from app.schemas import (
    AdminPrimeAdminDelegateRequest,
    AdminUserActiveUpdateRequest,
    AdminUserAdminUpdateRequest,
    AdminUserLiveTradingUpdateRequest,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def require_prime_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin or not current_user.is_prime_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="프라임 관리자 권한이 필요합니다.")
    return current_user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
    return current_user


def get_target_user_or_404(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="회원을 찾을 수 없습니다.")
    return user


def count_other_active_admins(db: Session, user_id: int) -> int:
    return (
        db.query(func.count(User.id))
        .filter(
            User.id != user_id,
            User.is_admin == True,
            User.is_active == True,
        )
        .scalar()
        or 0
    )


@router.get("/overview")
def admin_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    total_users = db.query(func.count(User.id)).scalar() or 0
    active_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar() or 0
    inactive_users = db.query(func.count(User.id)).filter(User.is_active == False).scalar() or 0
    admin_users = db.query(func.count(User.id)).filter(User.is_admin == True).scalar() or 0
    dry_run_users = db.query(func.count(User.id)).filter(User.dry_run == True).scalar() or 0
    live_allowed_users = db.query(func.count(User.id)).filter(User.enable_live_trading == True).scalar() or 0
    total_accounts = db.query(func.count(TradingAccount.id)).scalar() or 0
    active_accounts = db.query(func.count(TradingAccount.id)).filter(TradingAccount.is_active == True).scalar() or 0
    running_accounts = db.query(func.count(TradingAccount.id)).filter(TradingAccount.is_running == True).scalar() or 0
    total_logs = db.query(func.count(TradeLog.id)).scalar() or 0

    return {
        "status": "ok",
        "current_user_id": current_user.id,
        "summary": {
            "total_users": total_users,
            "active_users": active_users,
            "inactive_users": inactive_users,
            "admin_users": admin_users,
            "prime_admin_users": db.query(func.count(User.id)).filter(User.is_prime_admin == True).scalar() or 0,
            "dry_run_users": dry_run_users,
            "live_allowed_users": live_allowed_users,
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "running_accounts": running_accounts,
            "total_logs": total_logs,
        },
    }


@router.get("/users")
def admin_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    users = db.query(User).order_by(User.id.asc()).all()
    result = []

    for user in users:
        account_count = db.query(func.count(TradingAccount.id)).filter(TradingAccount.user_id == user.id).scalar() or 0
        active_account_count = (
            db.query(func.count(TradingAccount.id))
            .filter(TradingAccount.user_id == user.id, TradingAccount.is_active == True)
            .scalar()
            or 0
        )
        running_account_count = (
            db.query(func.count(TradingAccount.id))
            .filter(TradingAccount.user_id == user.id, TradingAccount.is_running == True)
            .scalar()
            or 0
        )
        log_count = db.query(func.count(TradeLog.id)).filter(TradeLog.user_id == user.id).scalar() or 0

        result.append({
            "id": user.id,
            "email": user.email,
            "dry_run": user.dry_run,
            "is_admin": user.is_admin,
            "is_prime_admin": user.is_prime_admin,
            "is_active": user.is_active,
            "enable_live_trading": user.enable_live_trading,
            "created_at": user.created_at,
            "account_count": account_count,
            "active_account_count": active_account_count,
            "running_account_count": running_account_count,
            "log_count": log_count,
        })

    return result


@router.patch("/users/{user_id}/live-trading")
def update_user_live_trading(
    user_id: int,
    body: AdminUserLiveTradingUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = get_target_user_or_404(db, user_id)

    if body.enable_live_trading and user.dry_run:
        raise HTTPException(
            status_code=400,
            detail="DRY RUN으로 가입한 회원은 실주문 허용을 켤 수 없습니다.",
        )

    if body.enable_live_trading and not user.is_active:
        raise HTTPException(
            status_code=400,
            detail="비활성 회원은 실주문 허용을 켤 수 없습니다.",
        )

    user.enable_live_trading = body.enable_live_trading
    db.commit()
    db.refresh(user)

    return {
        "status": "updated",
        "user_id": user.id,
        "enable_live_trading": user.enable_live_trading,
    }


@router.patch("/users/{user_id}/admin")
def update_user_admin(
    user_id: int,
    body: AdminUserAdminUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = get_target_user_or_404(db, user_id)

    if not body.is_admin and user.id == current_user.id:
        if count_other_active_admins(db, user.id) <= 0:
            raise HTTPException(
                status_code=400,
                detail="마지막 활성 관리자의 권한은 회수할 수 없습니다.",
            )

    if body.is_admin and not user.is_active:
        raise HTTPException(
            status_code=400,
            detail="비활성 회원에게 관리자 권한을 줄 수 없습니다.",
        )

    user.is_admin = body.is_admin
    db.commit()
    db.refresh(user)

    return {
        "status": "updated",
        "user_id": user.id,
        "is_admin": user.is_admin,
            "is_prime_admin": user.is_prime_admin,
    }


@router.patch("/users/{user_id}/active")
def update_user_active(
    user_id: int,
    body: AdminUserActiveUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = get_target_user_or_404(db, user_id)

    if not body.is_active and user.is_admin and count_other_active_admins(db, user.id) <= 0:
        raise HTTPException(
            status_code=400,
            detail="마지막 활성 관리자는 비활성화할 수 없습니다.",
        )

    user.is_active = body.is_active

    if not user.is_active:
        user.enable_live_trading = False
        if user.id == current_user.id:
            user.is_admin = False

        running_accounts = (
            db.query(TradingAccount)
            .filter(TradingAccount.user_id == user.id, TradingAccount.is_running == True)
            .all()
        )
        for account in running_accounts:
            account.is_running = False
            stop_account_job(account.id)

    db.commit()
    db.refresh(user)

    return {
        "status": "updated",
        "user_id": user.id,
        "is_active": user.is_active,
        "is_admin": user.is_admin,
            "is_prime_admin": user.is_prime_admin,
        "enable_live_trading": user.enable_live_trading,
    }


@router.patch("/prime-admin/delegate")
def delegate_prime_admin(
    body: AdminPrimeAdminDelegateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_prime_admin),
):
    target_user = db.query(User).filter(User.id == body.target_user_id).first()

    if not target_user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    if target_user.id == current_user.id:
        raise HTTPException(status_code=400, detail="이미 현재 계정이 프라임 관리자입니다.")

    if not target_user.is_active:
        raise HTTPException(status_code=400, detail="비활성 회원에게 위임할 수 없습니다.")

    if not target_user.is_admin:
        raise HTTPException(status_code=400, detail="관리자에게만 위임할 수 있습니다.")

    current_user.is_prime_admin = False

    target_user.is_prime_admin = True
    target_user.is_admin = True

    db.commit()

    return {"status": "ok"}
