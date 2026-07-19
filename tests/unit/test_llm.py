from prospector.agents.llm import LLM_TIMEOUT


def test_llm_timeout_allows_long_streaming_reads() -> None:
    assert LLM_TIMEOUT.connect == 10.0
    assert LLM_TIMEOUT.read == 600.0
    assert LLM_TIMEOUT.write == 120.0
    assert LLM_TIMEOUT.pool == 120.0
