import pytest
from data_platform.retrieval_engine.errors import RetrievalUnavailableError, FilterTooBroadError, LedgerUnavailableError


@pytest.mark.parametrize('cls,retryable', [(RetrievalUnavailableError, True), (FilterTooBroadError, False), (LedgerUnavailableError, True)])
def test_hint(cls, retryable):
    error = cls('失败')
    assert error.to_hint() == f'[{error.code}] 失败 | 建议: {error.suggestion}'
    assert error.retryable is retryable
