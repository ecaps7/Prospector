import pytest

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.config import clear_settings_cache


@pytest.fixture(autouse=True)
def require_credentials():
    clear_settings_cache()
    try:
        require_llm_settings()
    except LlmNotConfiguredError as exc:
        pytest.skip(str(exc))
    yield
    clear_settings_cache()
