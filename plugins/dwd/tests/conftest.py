"""Environment of the hub must be set before its code is imported (settings are frozen at first import)."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="dwd_plugin_tests_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/hub.db"
os.environ["SECRET_KEY"] = "pytest-secret"
os.environ["VAPID_AUTO"] = "false"
os.environ["DATA_DIR"] = _tmp
os.environ["AI_PROVIDER"] = "none"
# The family's location for the tests (any point in Germany; not real household data)
os.environ["WEATHER_LATITUDE"] = "50.11"
os.environ["WEATHER_LONGITUDE"] = "8.68"
for key in ("DWD_LATITUDE", "DWD_LONGITUDE", "DWD_MIN_SEVERITY", "DWD_NOTIFY", "DWD_POLLEN_REGION",
            "DWD_POLLEN_TYPES", "DWD_POLLEN_MIN"):
    os.environ.pop(key, None)

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from fh_plugin_dwd import dwd  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    dwd.clear_cache()
    from helpers import NOW
    from fh_plugin_dwd import plugin as plugin_module

    monkeypatch.setattr(plugin_module, "_now", lambda: NOW)  # the clock of the tests
    from app.plugins import api

    api.reset_limits()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield
    dwd.clear_cache()


@pytest.fixture
def http():
    with respx.mock(using="httpx", assert_all_called=False) as service:
        service.route(host="testserver").pass_through()
        yield service


@pytest.fixture
def db():
    from app.core.database import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        yield session
        from app.models.user import User

        session.query(User).delete()
        session.commit()


def ok(json):
    return httpx.Response(200, json=json)
