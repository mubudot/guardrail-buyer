import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

os.environ["GRB_NO_AUTOAPP"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.guardrails import Policy  # noqa: E402
from app.paypal_client import MockPayPal  # noqa: E402
from app.service import BuyerService, Catalog  # noqa: E402


@pytest.fixture
def policy():
    return Policy.load(ROOT / "policy.yaml")


@pytest.fixture
def catalog():
    return Catalog.load(ROOT / "data" / "catalog.json")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("UTC"))

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(policy, catalog, clock):
    return BuyerService(policy, catalog, MockPayPal(), ":memory:", clock=clock)
