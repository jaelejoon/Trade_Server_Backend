from sqlalchemy import BigInteger, Column, ForeignKey, Integer, Numeric, String

from app.db.base import Base


class SpotFill(Base):
    __tablename__ = "spot_fills"

    trading_account_id = Column(Integer, ForeignKey("trading_accounts.id"), primary_key=True)
    trade_id = Column(BigInteger, primary_key=True)
    order_id = Column(BigInteger, nullable=False)
    side = Column(String(4), nullable=False)
    quantity = Column(Numeric(32, 16), nullable=False)
    commission = Column(Numeric(32, 16), nullable=False)
    commission_asset = Column(String(16), nullable=False)
