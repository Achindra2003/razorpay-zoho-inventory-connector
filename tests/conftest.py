import pytest

from zoho_connector.config import Settings
from zoho_connector.runtime import demo_runtime


async def _no_sleep(_seconds: float) -> None:
    return None


def make_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(state_dir=tmp_path, max_wait_seconds=5.0, max_retries=3)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
async def runtime(settings):
    rt = await demo_runtime(settings)
    rt.client.sleep = _no_sleep  # retries/backoff without real waiting
    rt.client.limiter.sleep = _no_sleep
    yield rt
    await rt.aclose()


@pytest.fixture
def mock(runtime):
    return runtime.mock


@pytest.fixture
def tools(runtime):
    return runtime.tools
