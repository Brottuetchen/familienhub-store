import pytest

from fh_plugin_recap import build, store

RECAP_ENV = ("RECAP_JELLYFIN_URL", "RECAP_JELLYFIN_TOKEN", "RECAP_ABS_URL", "RECAP_IMMICH_URL",
             "RECAP_NAVIDROME_URL", "RECAP_NEXTCLOUD_URL")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Own data folder per test, no leftover settings or jobs."""
    for key in RECAP_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(store, "base_dir", lambda: tmp_path / "plugin-data" / "recap")
    build.reset()
    yield
    build.reset()
