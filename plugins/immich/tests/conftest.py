import pytest

from fh_plugin_immich import plugin as mod


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    mod.reset_state()
    for key in ("IMMICH_URL", "IMMICH_API_KEY", "IMMICH_ALBUM_IDS", "IMMICH_INCLUDE_ARCHIVED", "IMMICH_MAX_PHOTOS"):
        monkeypatch.delenv(key, raising=False)
    yield
    mod.reset_state()
