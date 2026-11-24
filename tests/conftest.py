import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet
import dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 개발자의 .env가 테스트 DB나 주문 차단 설정을 덮어쓰지 않게 한다.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ['ENVIRONMENT'] = 'dev'
os.environ['DATABASE_URL'] = os.environ.get('TEST_DATABASE_URL', 'postgresql://localhost/trade_isolated_test')
os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['DRY_RUN'] = 'true'
os.environ['ENABLE_LIVE_TRADING'] = 'false'
