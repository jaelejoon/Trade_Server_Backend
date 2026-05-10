from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.strategy_signal_service import get_v57_signal_from_db_candles

router = APIRouter(tags=["strategy"])


@router.get("/strategy/v57/signal/{symbol}")
def get_v57_signal(
    symbol: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_v57_signal_from_db_candles(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
        sync_recent=True,
    )
