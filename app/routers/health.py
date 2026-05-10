from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import BINANCE_TESTNET, DRY_RUN
from app.db.session import get_db
from app.services.binance_service import check_server_time_drift

router = APIRouter(tags=['health'])


@router.get('/health')
def health():
    return {'status': 'ok', 'dry_run': DRY_RUN, 'binance_testnet': BINANCE_TESTNET}


@router.get('/health/db')
def health_db(db: Session = Depends(get_db)):
    db.execute(text('SELECT 1'))
    return {'status': 'ok'}


@router.get('/health/binance')
def health_binance():
    return check_server_time_drift()
