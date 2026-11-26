from sqlalchemy.orm import Session

from app.core.config import ENABLE_LIVE_TRADING
from app.models.app_setting import AppSetting

LIVE_TRADING_KEY = "enable_live_trading"


def _bool_to_db(value: bool) -> str:
    return "true" if value else "false"


def _db_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_setting(db: Session, key: str) -> AppSetting | None:
    return db.query(AppSetting).filter(AppSetting.key == key).first()


def get_live_trading_enabled(db: Session) -> bool:
    setting = get_setting(db, LIVE_TRADING_KEY)
    if setting is None:
        return ENABLE_LIVE_TRADING
    return _db_to_bool(setting.value, default=False)


def set_live_trading_enabled(db: Session, enabled: bool) -> bool:
    setting = get_setting(db, LIVE_TRADING_KEY)
    if setting is None:
        setting = AppSetting(key=LIVE_TRADING_KEY, value=_bool_to_db(enabled))
        db.add(setting)
    else:
        setting.value = _bool_to_db(enabled)

    db.commit()
    db.refresh(setting)
    return _db_to_bool(setting.value, default=False)


def ensure_default_live_trading_setting(db: Session) -> None:
    if get_setting(db, LIVE_TRADING_KEY) is not None:
        return

    db.add(AppSetting(key=LIVE_TRADING_KEY, value=_bool_to_db(ENABLE_LIVE_TRADING)))
    db.commit()
