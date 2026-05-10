from typing import Optional

from sqlalchemy.orm import Session

from app.models.error_log import ErrorLog


def create_error_log(
    db: Session,
    source: str,
    message: str,
    user_id: Optional[int] = None,
    trading_account_id: Optional[int] = None,
    level: str = 'ERROR',
    raw_data: Optional[str] = None,
):
    log = ErrorLog(
        user_id=user_id,
        trading_account_id=trading_account_id,
        level=level,
        source=source,
        message=message,
        raw_data=raw_data,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
