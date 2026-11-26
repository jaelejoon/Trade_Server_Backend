from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.routers.admin import require_admin
from app.schemas import StrategyCreateRequest, StrategySelectRequest, StrategyConfigUpdateRequest
from app.services.trading_strategy_service import (
    create_admin_strategy,
    get_effective_strategy_for_user,
    get_strategy_or_404,
    list_active_strategies,
    list_user_strategy_histories,
    merge_config,
    seed_builtin_strategies,
    set_user_strategy_setting,
    validate_strategy_config,
)

router = APIRouter(prefix="/strategies", tags=["strategies"])


def serialize_strategy(strategy, include_config: bool = True):
    result = {
        "id": strategy.id,
        "code": strategy.code,
        "name": strategy.name,
        "version": strategy.version,
        "description": strategy.description,
        "engine_key": strategy.engine_key,
        "status": strategy.status,
        "editable_fields": strategy.editable_fields or [],
        "is_builtin": strategy.is_builtin,
        "created_at": strategy.created_at,
        "updated_at": strategy.updated_at,
    }
    if include_config:
        result["default_config"] = strategy.default_config or {}
    return result


def serialize_setting(setting, effective_config: dict):
    return {
        "id": setting.id,
        "user_id": setting.user_id,
        "trading_account_id": setting.trading_account_id,
        "strategy_id": setting.strategy_id,
        "custom_config": setting.custom_config or {},
        "effective_config": effective_config,
        "is_active": setting.is_active,
        "created_at": setting.created_at,
        "updated_at": setting.updated_at,
        "strategy": serialize_strategy(setting.strategy, include_config=True),
    }


def serialize_history(history):
    return {
        "id": history.id,
        "user_id": history.user_id,
        "trading_account_id": history.trading_account_id,
        "strategy_id": history.strategy_id,
        "setting_id": history.setting_id,
        "action": history.action,
        "before_strategy_id": history.before_strategy_id,
        "before_config": history.before_config or {},
        "after_config": history.after_config or {},
        "changed_fields": history.changed_fields or [],
        "created_at": history.created_at,
        "strategy": serialize_strategy(history.strategy, include_config=False) if history.strategy else None,
    }


@router.get("")
def get_strategies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    strategies = list_active_strategies(db)
    return [serialize_strategy(strategy, include_config=True) for strategy in strategies]


@router.get("/me/active")
def get_my_active_strategy(
    trading_account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    strategy, effective_config, setting = get_effective_strategy_for_user(
        db=db,
        user_id=current_user.id,
        trading_account_id=trading_account_id,
    )

    return {
        "strategy": serialize_strategy(strategy, include_config=True),
        "setting_id": setting.id if setting else None,
        "trading_account_id": setting.trading_account_id if setting else trading_account_id,
        "custom_config": setting.custom_config if setting else {},
        "effective_config": effective_config,
        "source": "user_setting" if setting else "default_builtin",
    }


@router.post("/me/select")
def select_my_strategy(
    body: StrategySelectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    setting = set_user_strategy_setting(
        db=db,
        user_id=current_user.id,
        strategy_id=body.strategy_id,
        trading_account_id=body.trading_account_id,
        custom_config=body.custom_config,
    )
    effective_config = merge_config(setting.strategy, setting.custom_config)

    return {
        "status": "selected",
        "setting": serialize_setting(setting, effective_config),
    }


@router.patch("/me/config")
def update_my_strategy_config(
    body: StrategyConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    strategy, _, setting = get_effective_strategy_for_user(
        db=db,
        user_id=current_user.id,
        trading_account_id=body.trading_account_id,
    )

    if setting is None:
        setting = set_user_strategy_setting(
            db=db,
            user_id=current_user.id,
            strategy_id=strategy.id,
            trading_account_id=body.trading_account_id,
            custom_config=body.custom_config,
        )
    else:
        before_config = setting.custom_config or {}
        setting.custom_config = validate_strategy_config(setting.strategy, body.custom_config)
        from app.services.trading_strategy_service import create_strategy_history
        create_strategy_history(
            db=db,
            user_id=current_user.id,
            trading_account_id=body.trading_account_id,
            strategy_id=setting.strategy_id,
            setting_id=setting.id,
            action="config_update",
            before_strategy_id=setting.strategy_id,
            before_config=before_config,
            after_config=setting.custom_config,
        )
        db.commit()
        db.refresh(setting)

    effective_config = merge_config(setting.strategy, setting.custom_config)
    return {
        "status": "updated",
        "setting": serialize_setting(setting, effective_config),
    }


@router.get("/me/history")
def get_my_strategy_history(
    trading_account_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    histories = list_user_strategy_histories(
        db=db,
        user_id=current_user.id,
        trading_account_id=trading_account_id,
        limit=limit,
    )
    return [serialize_history(history) for history in histories]


@router.post("/admin")
def create_strategy_by_admin(
    body: StrategyCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    strategy = create_admin_strategy(
        db=db,
        current_user_id=current_user.id,
        code=body.code,
        name=body.name,
        version=body.version,
        description=body.description,
        default_config=body.default_config,
        editable_fields=body.editable_fields,
    )
    return {
        "status": "created",
        "strategy": serialize_strategy(strategy, include_config=True),
    }


@router.get("/{strategy_id}")
def get_strategy_detail(
    strategy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    seed_builtin_strategies(db)
    strategy = get_strategy_or_404(db, strategy_id)
    return serialize_strategy(strategy, include_config=True)
