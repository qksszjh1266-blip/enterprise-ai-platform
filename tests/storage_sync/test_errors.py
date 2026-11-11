import pytest
from data_platform.storage_sync.errors import ShadowWriteError, VersionConflictError, RollbackError, GarbageCollectionError

@pytest.mark.parametrize('cls', [ShadowWriteError, VersionConflictError, RollbackError, GarbageCollectionError])
def test_hint(cls):
    hint = cls('测试错误', doc_id='sop', version=2).to_hint()
    assert hint == f'[{cls.code}] doc_id=sop version=2 | 测试错误 | 建议: {cls.suggestion}'
    assert 'doc_id=- version=-' in cls('error').to_hint()
