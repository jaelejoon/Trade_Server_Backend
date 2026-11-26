import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
import dotenv
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 개발자의 .env가 테스트 DB나 주문 차단 설정을 덮어쓰지 않게 한다.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ['ENVIRONMENT'] = 'dev'
os.environ['DATABASE_URL'] = os.environ.get('TEST_DATABASE_URL', 'postgresql://localhost/trade_isolated_test')
os.environ['JWT_SECRET_KEY'] = secrets.token_hex(32)
os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['DRY_RUN'] = 'true'
os.environ['ENABLE_LIVE_TRADING'] = 'false'

os.environ['BINANCE_TESTNET'] = 'true'
os.environ.pop('BINANCE_SPOT_BASE_URL', None)


@pytest.fixture
def allow_spot_testnet_reads(monkeypatch):
    from urllib.parse import urlsplit

    send = requests.Session.send

    def testnet_read_only(session, request, **kwargs):
        url = urlsplit(request.url)
        if ((url.scheme, url.netloc) != ("https", "testnet.binance.vision")
                or request.method != "GET"
                or url.path not in {"/api/v3/account", "/api/v3/openOrderList"}):
            pytest.fail("Only Spot Testnet read requests are allowed")
        return send(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", testnet_read_only)


@pytest.fixture(autouse=True)
def block_exchange_http(monkeypatch, request):
    if "allow_spot_testnet_reads" in request.fixturenames:
        return

    def reject_http(*args, **kwargs):
        pytest.fail('Unexpected outbound HTTP in a local test')

    monkeypatch.setattr(requests.Session, 'send', reject_http)
