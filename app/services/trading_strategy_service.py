from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.trading_strategy import StrategySettingHistory, TradingStrategy, UserStrategySetting
from app.models.trading_account import TradingAccount
from app.strategy.strategy_v57 import DEFAULT_V57_CONFIG


V57_EDITABLE_FIELDS = [
    "B_RSI_MIN",
    "B_RSI_MAX",
    "B_ATR_PCT_MIN",
    "B_BB_WIDTH_MIN",
    "B_STOP_ATR_MULT",
    "B_TAKE_PROFIT_R",
    "B_RISK_STRONG",
    "B_RISK_NORMAL",
    "B_MAX_POS_STRONG",
    "B_MAX_POS_NORMAL",
    "B_MAX_HOLD_BARS",









    "ENABLE_BAD_HOUR_FILTER",
    "BAD_ENTRY_HOURS_1H",
    "ENABLE_EVENT_FILTER",
    "BLOCK_ENTRY_ON_EXTREME_VOLATILITY",
    "EVENT_RISK_REDUCE_POSITION",
    "EVENT_RISK_REDUCTION_MULT",
]


NUMERIC_RANGES = {
    "B_RSI_MIN": (0, 100),
    "B_RSI_MAX": (0, 100),


    "B_ATR_PCT_MIN": (0, 1),
    "B_BB_WIDTH_MIN": (0, 1),


    "B_STOP_ATR_MULT": (0.1, 10),

    "B_TAKE_PROFIT_R": (0.1, 50),

    "B_RISK_STRONG": (0, 0.2),
    "B_RISK_NORMAL": (0, 0.2),

    "B_MAX_POS_STRONG": (0, 2),
    "B_MAX_POS_NORMAL": (0, 2),

    "B_MAX_HOLD_BARS": (1, 1000),

    "EVENT_RISK_REDUCTION_MULT": (0, 1),
}


BUILTIN_STRATEGY_DEFINITIONS = [
    {
        "code": "V57_LONG_SAFE_PUSH",
        "name": "V57 기본형",
        "version": "1",
        "description": "기존 V57 전략입니다. 롱 강화, 숏 정제, 청산 방어 로직을 포함한 기본 운영형 설정입니다.",
        "engine_key": "V57_LONG_SAFE_PUSH",
        "config_overrides": {},
    },
    {
        "code": "V57_CONSERVATIVE",
        "name": "V57 보수형",
        "version": "1",
        "description": "V57 엔진을 사용하지만 1회 리스크와 포지션 크기를 줄인 보수형 전략입니다. 실거래 전 안정성 확인용으로 적합합니다.",
        "engine_key": "V57_LONG_SAFE_PUSH",
        "config_overrides": {
            "B_RISK_STRONG": 0.025,
            "B_RISK_NORMAL": 0.018,
            "B_MAX_POS_STRONG": 0.55,
            "B_MAX_POS_NORMAL": 0.40,


            "MAX_TOTAL_POSITION_RATIO": 1.00,
            "MAX_TOTAL_RISK_RATIO": 0.045,
            "DAILY_DD_STOP_PCT": -0.025,
            "MONTHLY_DD_STOP_PCT": -0.07,
            "PEAK_DD_STOP_PCT": -0.12,
        },
    },
    {
        "code": "V57_AGGRESSIVE_LONG",
        "name": "V57 롱 공격형",
        "version": "1",
        "description": "상승장 롱 비중을 더 키운 공격형 전략입니다. 수익 기회는 커질 수 있지만 MDD와 연속 손실 위험도 커집니다.",
        "engine_key": "V57_LONG_SAFE_PUSH",
        "config_overrides": {
            "B_RISK_STRONG": 0.055,
            "B_RISK_NORMAL": 0.038,
            "B_MAX_POS_STRONG": 1.00,
            "B_MAX_POS_NORMAL": 0.75,
            "B_RSI_MIN": 53,
            "B_RSI_MAX": 84,
            "B_TAKE_PROFIT_R": 9.50,


            "MAX_TOTAL_POSITION_RATIO": 1.75,
            "MAX_TOTAL_RISK_RATIO": 0.10,
        },
    },
]


def build_changed_fields(before_config: dict[str, Any] | None, after_config: dict[str, Any] | None) -> list[str]:
    before = before_config or {}
    after = after_config or {}
    keys = set(before.keys()) | set(after.keys())
    return sorted([key for key in keys if before.get(key) != after.get(key)])


def create_strategy_history(
    db: Session,
    user_id: int,
    trading_account_id: int | None,
    strategy_id: int,
    setting_id: int | None,
    action: str,
    before_strategy_id: int | None,
    before_config: dict[str, Any] | None,
    after_config: dict[str, Any] | None,
) -> None:
    history = StrategySettingHistory(
        user_id=user_id,
        trading_account_id=trading_account_id,
        strategy_id=strategy_id,
        setting_id=setting_id,
        action=action,
        before_strategy_id=before_strategy_id,
        before_config=before_config or {},
        after_config=after_config or {},
        changed_fields=build_changed_fields(before_config, after_config),
    )
    db.add(history)


def list_user_strategy_histories(
    db: Session,
    user_id: int,
    trading_account_id: int | None = None,
    limit: int = 20,
) -> list[StrategySettingHistory]:
    query = (
        db.query(StrategySettingHistory)
        .filter(StrategySettingHistory.user_id == user_id)
        .order_by(StrategySettingHistory.id.desc())
    )

    if trading_account_id is None:
        query = query.filter(StrategySettingHistory.trading_account_id.is_(None))
    else:
        query = query.filter(StrategySettingHistory.trading_account_id == trading_account_id)

    return query.limit(limit).all()


def build_builtin_config(config_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_V57_CONFIG)
    config.update(config_overrides or {})
    return config


def seed_builtin_strategies(db: Session) -> TradingStrategy:
    first_strategy: TradingStrategy | None = None

    for definition in BUILTIN_STRATEGY_DEFINITIONS:
        strategy = (
            db.query(TradingStrategy)
            .filter(
                TradingStrategy.code == definition["code"],
                TradingStrategy.version == definition["version"],
            )
            .first()
        )

        builtin_config = build_builtin_config(definition.get("config_overrides"))

        if strategy is None:
            strategy = TradingStrategy(
                code=definition["code"],
                name=definition["name"],
                version=definition["version"],
                description=definition["description"],
                engine_key=definition["engine_key"],
                status="active",
                default_config=builtin_config,
                editable_fields=V57_EDITABLE_FIELDS,
                is_builtin=True,
            )
            db.add(strategy)
            db.flush()
        else:
            strategy.name = definition["name"]
            strategy.description = definition["description"]
            strategy.engine_key = definition["engine_key"]
            strategy.status = "active"
            strategy.is_builtin = True
            strategy.default_config = builtin_config
            strategy.editable_fields = strategy.editable_fields or V57_EDITABLE_FIELDS

        if first_strategy is None and definition["code"] == "V57_LONG_SAFE_PUSH":
            first_strategy = strategy

    db.commit()

    fallback = (
        db.query(TradingStrategy)
        .filter(TradingStrategy.code == "V57_LONG_SAFE_PUSH", TradingStrategy.version == "1")
        .first()
    )
    if fallback is None:
        raise HTTPException(status_code=500, detail="기본 전략 시드 생성에 실패했습니다.")
    return fallback

def list_active_strategies(db: Session):
    seed_builtin_strategies(db)
    return (
        db.query(TradingStrategy)
        .filter(TradingStrategy.status == "active")
        .order_by(TradingStrategy.id.asc())
        .all()
    )


def get_strategy_or_404(db: Session, strategy_id: int) -> TradingStrategy:
    strategy = db.query(TradingStrategy).filter(TradingStrategy.id == strategy_id).first()
    if strategy is None:
        raise HTTPException(status_code=404, detail="전략을 찾을 수 없습니다.")
    return strategy


def validate_strategy_config(strategy: TradingStrategy, config: dict[str, Any]) -> dict[str, Any]:
    if config is None:
        config = {}

    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="custom_config는 JSON object여야 합니다.")

    editable_fields = set(strategy.editable_fields or [])
    unknown_fields = sorted(set(config.keys()) - editable_fields)
    if unknown_fields:
        raise HTTPException(
            status_code=400,
            detail={"message": "수정할 수 없는 전략 파라미터가 포함되어 있습니다.", "fields": unknown_fields},
        )

    clean = {}
    for key, value in config.items():
        default_value = (strategy.default_config or {}).get(key)

        if isinstance(default_value, bool):
            if not isinstance(value, bool):
                raise HTTPException(status_code=400, detail=f"{key}는 true/false 값이어야 합니다.")
            clean[key] = value
            continue

        if isinstance(default_value, list):
            if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
                raise HTTPException(status_code=400, detail=f"{key}는 정수 배열이어야 합니다.")
            clean[key] = value
            continue

        if key in NUMERIC_RANGES:
            if not isinstance(value, (int, float)):
                raise HTTPException(status_code=400, detail=f"{key}는 숫자여야 합니다.")
            min_value, max_value = NUMERIC_RANGES[key]
            if value < min_value or value > max_value:
                raise HTTPException(status_code=400, detail=f"{key}는 {min_value} 이상 {max_value} 이하여야 합니다.")
            clean[key] = value
            continue

        clean[key] = value

    if "B_RSI_MIN" in clean or "B_RSI_MAX" in clean:
        b_min = clean.get("B_RSI_MIN", (strategy.default_config or {}).get("B_RSI_MIN"))
        b_max = clean.get("B_RSI_MAX", (strategy.default_config or {}).get("B_RSI_MAX"))
        if b_min > b_max:
            raise HTTPException(status_code=400, detail="B_RSI_MIN은 B_RSI_MAX보다 클 수 없습니다.")

    return clean


def merge_config(strategy: TradingStrategy, custom_config: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = deepcopy(strategy.default_config or {})
    merged.update(custom_config or {})
    return merged


def get_active_user_strategy_setting(db: Session, user_id: int, trading_account_id: int | None = None) -> UserStrategySetting | None:
    query = (
        db.query(UserStrategySetting)
        .join(TradingStrategy, TradingStrategy.id == UserStrategySetting.strategy_id)
        .filter(
            UserStrategySetting.user_id == user_id,
            UserStrategySetting.is_active == True,
            TradingStrategy.status == "active",
        )
    )

    if trading_account_id is not None:
        account_setting = (
            query.filter(UserStrategySetting.trading_account_id == trading_account_id)
            .order_by(UserStrategySetting.id.desc())
            .first()
        )
        if account_setting is not None:
            return account_setting

    return (
        query.filter(UserStrategySetting.trading_account_id.is_(None))
        .order_by(UserStrategySetting.id.desc())
        .first()
    )


def get_effective_strategy_for_user(db: Session, user_id: int, trading_account_id: int | None = None) -> tuple[TradingStrategy, dict[str, Any], UserStrategySetting | None]:
    seed_builtin_strategies(db)
    setting = get_active_user_strategy_setting(db, user_id=user_id, trading_account_id=trading_account_id)

    if setting is not None:
        return setting.strategy, merge_config(setting.strategy, setting.custom_config), setting

    strategy = (
        db.query(TradingStrategy)
        .filter(TradingStrategy.code == "V57_LONG_SAFE_PUSH", TradingStrategy.version == "1")
        .first()
    )
    if strategy is None:
        strategy = seed_builtin_strategies(db)

    return strategy, merge_config(strategy, {}), None


def set_user_strategy_setting(
    db: Session,
    user_id: int,
    strategy_id: int,
    custom_config: dict[str, Any] | None,
    trading_account_id: int | None = None,
) -> UserStrategySetting:
    strategy = get_strategy_or_404(db, strategy_id)
    if strategy.status != "active":
        raise HTTPException(status_code=400, detail="활성화된 전략만 선택할 수 있습니다.")

    if trading_account_id is not None:
        account = (
            db.query(TradingAccount)
            .filter(TradingAccount.id == trading_account_id, TradingAccount.user_id == user_id, TradingAccount.is_active == True)
            .first()
        )
        if account is None:
            raise HTTPException(status_code=404, detail="자동매매 계정을 찾을 수 없습니다.")

    clean_config = validate_strategy_config(strategy, custom_config or {})

    query = db.query(UserStrategySetting).filter(
        UserStrategySetting.user_id == user_id,
        UserStrategySetting.is_active == True,
    )
    if trading_account_id is None:
        query = query.filter(UserStrategySetting.trading_account_id.is_(None))
    else:
        query = query.filter(UserStrategySetting.trading_account_id == trading_account_id)

    old_settings = query.all()
    previous_setting = old_settings[0] if old_settings else None
    before_strategy_id = previous_setting.strategy_id if previous_setting else None
    before_config = previous_setting.custom_config if previous_setting else {}

    for old_setting in old_settings:
        old_setting.is_active = False

    setting = UserStrategySetting(
        user_id=user_id,
        trading_account_id=trading_account_id,
        strategy_id=strategy.id,
        custom_config=clean_config,
        is_active=True,
    )
    db.add(setting)
    db.flush()

    action = "strategy_select"
    if before_strategy_id == strategy.id:
        action = "config_update"
    if before_strategy_id is None and not clean_config:
        action = "strategy_select"

    create_strategy_history(
        db=db,
        user_id=user_id,
        trading_account_id=trading_account_id,
        strategy_id=strategy.id,
        setting_id=setting.id,
        action=action,
        before_strategy_id=before_strategy_id,
        before_config=before_config,
        after_config=clean_config,
    )

    db.commit()
    db.refresh(setting)
    return setting


def create_admin_strategy(
    db: Session,
    current_user_id: int,
    code: str,
    name: str,
    version: str,
    description: str | None,
    default_config: dict[str, Any],
    editable_fields: list[str] | None,
) -> TradingStrategy:
    base_config = deepcopy(DEFAULT_V57_CONFIG)
    base_config.update(default_config or {})
    duplicated = (
        db.query(TradingStrategy)
        .filter(TradingStrategy.code == code, TradingStrategy.version == version)
        .first()
    )
    if duplicated is not None:
        raise HTTPException(status_code=400, detail="이미 같은 code/version 전략이 존재합니다.")

    strategy = TradingStrategy(
        code=code,
        name=name,
        version=version,
        description=description,
        engine_key="V57_LONG_SAFE_PUSH",
        status="active",
        default_config=base_config,
        editable_fields=editable_fields or V57_EDITABLE_FIELDS,
        is_builtin=False,
        created_by_user_id=current_user_id,
    )
    db.add(strategy)
    db.commit()
    db.refresh(strategy)
    return strategy
