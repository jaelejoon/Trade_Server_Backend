from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.security import now_utc
from app.db.base import Base


class TradingStrategy(Base):
    __tablename__ = "trading_strategies"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_trading_strategies_code_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    version = Column(String(50), nullable=False, default="1")
    description = Column(Text, nullable=True)
    engine_key = Column(String(100), nullable=False, default="V57_LONG_SAFE_PUSH")
    status = Column(String(30), nullable=False, default="active")
    default_config = Column(JSON, nullable=False, default=dict)
    editable_fields = Column(JSON, nullable=False, default=list)
    is_builtin = Column(Boolean, nullable=False, default=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=now_utc)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    user_settings = relationship("UserStrategySetting", back_populates="strategy")


class UserStrategySetting(Base):
    __tablename__ = "user_strategy_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trading_account_id = Column(Integer, ForeignKey("trading_accounts.id"), nullable=True, index=True)
    strategy_id = Column(Integer, ForeignKey("trading_strategies.id"), nullable=False, index=True)
    custom_config = Column(JSON, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=now_utc)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    strategy = relationship("TradingStrategy", back_populates="user_settings")


class StrategySettingHistory(Base):
    __tablename__ = "strategy_setting_histories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trading_account_id = Column(Integer, ForeignKey("trading_accounts.id"), nullable=True, index=True)
    strategy_id = Column(Integer, ForeignKey("trading_strategies.id"), nullable=False, index=True)
    setting_id = Column(Integer, ForeignKey("user_strategy_settings.id"), nullable=True, index=True)
    action = Column(String(50), nullable=False)
    before_strategy_id = Column(Integer, nullable=True)
    before_config = Column(JSON, nullable=False, default=dict)
    after_config = Column(JSON, nullable=False, default=dict)
    changed_fields = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=now_utc, index=True)

    strategy = relationship("TradingStrategy")
    setting = relationship("UserStrategySetting")


class StrategyBacktestRun(Base):
    __tablename__ = "strategy_backtest_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trading_account_id = Column(Integer, ForeignKey("trading_accounts.id"), nullable=True, index=True)
    strategy_id = Column(Integer, ForeignKey("trading_strategies.id"), nullable=False, index=True)
    setting_id = Column(Integer, ForeignKey("user_strategy_settings.id"), nullable=True, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    source_candle_count = Column(Integer, nullable=False, default=0)
    signal_count = Column(Integer, nullable=False, default=0)
    trade_count = Column(Integer, nullable=False, default=0)
    final_equity = Column(Float, nullable=False, default=0)
    return_pct = Column(Float, nullable=False, default=0)
    mdd = Column(Float, nullable=False, default=0)
    win_rate = Column(Float, nullable=False, default=0)
    pf = Column(Float, nullable=False, default=0)
    liquidation_count = Column(Integer, nullable=False, default=0)
    config_snapshot = Column(JSON, nullable=False, default=dict)
    result_summary = Column(JSON, nullable=False, default=dict)
    diagnostics = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="success")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc, index=True)

    strategy = relationship("TradingStrategy")
    setting = relationship("UserStrategySetting")
