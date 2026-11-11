import asyncio
import ast
from pathlib import Path
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from data_platform.storage_sync.models import KBDocument, KBParentChunk, DocumentStatus, SyncOutcome, DocumentIngestRequest
from data_platform.storage_sync.ports import VectorStorePort, KeywordStorePort, EmbeddingPort
from tests.fakes import InMemoryVectorStore, InMemoryKeywordStore, FakeEmbedder


async def state(manager, doc_id='sop'):
    async with manager.session_factory() as session:
        doc = await session.get(KBDocument, doc_id)
        parents = list((await session.scalars(select(KBParentChunk).where(KBParentChunk.doc_id == doc_id))).all())
        return doc, parents


async def publish(manager, req):
    result = await manager.sync_document(req)
    assert result.outcome == SyncOutcome.PUBLISHED, result.error_hint
    await manager.aclose()
    return result


async def assert_rollback(manager, req, result, prefix='SHADOW_WRITE_FAILED'):
    assert result.outcome == SyncOutcome.FAILED
    assert result.error_hint.startswith(f'[{prefix}]')
    doc, parents = await state(manager)
    assert doc.current_version == result.current_version == 1
    assert doc.status == DocumentStatus.FAILED
    expected = manager._build_chunks(req, 1)
    assert {p.parent_id for p in parents} == {p.parent_id for p in expected.parents}
    for store in (manager.vector_store, manager.keyword_store):
        assert set(store.data) == {c.child_id for c in expected.children}
        assert all(c.version == 1 for c in store.data.values())


async def test_01_first_publish(manager, req):
    result = await publish(manager, req)
    doc, parents = await state(manager)
    expected = manager._build_chunks(req, 1)
    assert result.current_version == doc.current_version == 1
    assert doc.status == DocumentStatus.ACTIVE
    assert len(parents) == result.parent_count == len(expected.parents)
    assert len(manager.vector_store.data) == len(manager.keyword_store.data) == result.child_count == len(expected.children)
    for c in manager.vector_store.data.values():
        assert c.dept_id == req.dept_id and c.allowed_roles == req.allowed_roles
        assert len(c.embedding) == 8


async def test_02_hash_skip(manager, req, monkeypatch):
    await publish(manager, req)
    for s in (manager.vector_store, manager.keyword_store):
        s.calls.clear()
    async def forbidden(*args):
        pytest.fail('PG write on unchanged document')
    monkeypatch.setattr(manager, '_write_parent_chunks', forbidden)
    result = await manager.sync_document(req)
    assert result.outcome == SyncOutcome.SKIPPED_UNCHANGED
    assert result.previous_version == result.current_version == 1
    assert not manager.vector_store.calls and not manager.keyword_store.calls


async def test_03_force_reindex(manager, req):
    await publish(manager, req)
    assert (await publish(manager, req.model_copy(update={'force_reindex':True}))).current_version == 2


@pytest.mark.parametrize('change', [{'allowed_roles':['finance']}, {'dept_id':'finance'}])
async def test_04_acl_changes(manager, req, change):
    await publish(manager, req)
    changed = req.model_copy(update=change)
    assert (await publish(manager, changed)).current_version == 2
    assert all(c.allowed_roles == changed.allowed_roles and c.dept_id == changed.dept_id for c in manager.vector_store.data.values())


async def test_05_content_gc(manager, req):
    await publish(manager, req)
    await publish(manager, req.model_copy(update={'content':req.content+'\n更新'}))
    doc, parents = await state(manager)
    assert doc.current_version == 2
    assert {p.version for p in parents} == {2}
    assert {c.version for c in manager.vector_store.data.values()} == {2}
    assert {c.version for c in manager.keyword_store.data.values()} == {2}


async def test_06_vector_failure(manager, req):
    await publish(manager, req)
    manager.vector_store.fail_on.add('upsert_children')
    result = await manager.sync_document(req.model_copy(update={'force_reindex':True}))
    await assert_rollback(manager, req, result)


async def test_07_partial_keyword_failure(manager, req):
    await publish(manager, req)
    manager.keyword_store.fail_after_n = 1
    await assert_rollback(manager, req, await manager.sync_document(req.model_copy(update={'force_reindex':True})))


async def test_08_parent_failure(manager, req, monkeypatch):
    await publish(manager, req)
    async def fail(*args):
        raise RuntimeError('PG unavailable')
    monkeypatch.setattr(manager, '_write_parent_chunks', fail)
    await assert_rollback(manager, req, await manager.sync_document(req.model_copy(update={'force_reindex':True})))


async def test_09_rollback_failure(manager, req):
    await publish(manager, req)
    manager.keyword_store.fail_after_n = 1
    manager.keyword_store.fail_on.add('delete_version')
    result = await manager.sync_document(req.model_copy(update={'force_reindex':True}))
    assert result.outcome == SyncOutcome.FAILED
    assert result.error_hint.startswith('[ROLLBACK_FAILED]') and 'partial write' in result.error_hint
    assert (await state(manager))[0].current_version == 1
    assert any(c.version == 2 for c in manager.keyword_store.data.values())
    manager.keyword_store.fail_after_n = None
    manager.keyword_store.fail_on.clear()
    assert (await publish(manager, req.model_copy(update={'force_reindex':True}))).current_version == 2
    assert {c.version for c in manager.keyword_store.data.values()} == {2}


async def test_10_cas_conflict(manager, req, monkeypatch):
    await publish(manager, req)
    original = manager._write_shadow_version
    async def conflict(*args):
        await original(*args)
        async with manager.session_factory.begin() as session:
            await session.execute(update(KBDocument).where(KBDocument.doc_id == req.doc_id).values(current_version=5, status=DocumentStatus.ACTIVE))
    monkeypatch.setattr(manager, '_write_shadow_version', conflict)
    result = await manager.sync_document(req.model_copy(update={'force_reindex':True}))
    assert result.error_hint.startswith('[VERSION_CONFLICT]')
    assert result.current_version == 5
    doc, parents = await state(manager)
    assert doc.current_version == 5 and doc.status == DocumentStatus.ACTIVE
    assert all(p.version != 2 for p in parents)
    assert all(c.version != 2 for s in (manager.vector_store, manager.keyword_store) for c in s.data.values())


async def test_11_same_document_concurrent(manager, req):
    manager.vector_store.delay = .01
    results = await asyncio.gather(manager.sync_document(req), manager.sync_document(req.model_copy(update={'content':req.content+'new'})))
    assert [r.current_version for r in results] == [1,2]
    assert all(r.outcome == SyncOutcome.PUBLISHED for r in results)
    await manager.aclose()
    assert (await state(manager))[0].current_version == 2
    assert {c.version for c in manager.vector_store.data.values()} == {2}


async def test_12_different_documents_concurrent(manager, req):
    manager.vector_store.delay = .03
    results = await asyncio.gather(*(manager.sync_document(req.model_copy(update={'doc_id':f'doc-{i}'})) for i in range(10)))
    assert all(r.current_version == 1 and r.outcome == SyncOutcome.PUBLISHED for r in results)
    assert manager.vector_store.max_in_flight > 1
    await manager.aclose()
    for i in range(10):
        doc, parents = await state(manager, f'doc-{i}')
        assert doc.current_version == 1 and parents


async def test_13_gc_overtaken(manager, req):
    manager.gc_enabled = False
    await publish(manager, req)
    await publish(manager, req.model_copy(update={'content':req.content+'v2'}))
    await publish(manager, req.model_copy(update={'content':req.content+'v3'}))
    before = dict(manager.vector_store.data)
    await manager._garbage_collect(req.doc_id, 2)
    assert manager.vector_store.data == before
    assert {p.version for p in (await state(manager))[1]} == {1,2,3}
    await manager._garbage_collect(req.doc_id, 3)
    assert {p.version for p in (await state(manager))[1]} == {3}


async def test_14_gc_failure(manager, req, caplog):
    await publish(manager, req)
    manager.vector_store.fail_on.add('delete_stale_versions')
    result = await publish(manager, req.model_copy(update={'content':req.content+'update'}))
    assert result.current_version == 2
    assert (await state(manager))[0].status == DocumentStatus.ACTIVE
    assert '[GC_FAILED]' in caplog.text


async def test_15_bad_dimension(manager, req):
    manager.embedder.wrong_dimension = True
    result = await manager.sync_document(req)
    assert result.outcome == SyncOutcome.FAILED
    doc, parents = await state(manager)
    assert doc.current_version == 0 and doc.status == DocumentStatus.FAILED
    assert not parents and not manager.vector_store.data and not manager.keyword_store.data


def test_16_hash_normalization():
    from data_platform.storage_sync.sync_manager import VersionedSyncManager
    assert VersionedSyncManager.compute_content_hash('a\r\nb  ') == VersionedSyncManager.compute_content_hash('a\nb')


def test_17_pydantic_v2(req):
    with pytest.raises(ValidationError):
        DocumentIngestRequest(**(req.model_dump() | {'unexpected':True}))
    for package in ['data_platform','eval_platform','agent_matrix']:
        for path in Path(package).rglob('*.py'):
            tree = ast.parse(path.read_text())
            assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'dict' for n in ast.walk(tree))
            assert not any(isinstance(n, ast.Name) and n.id == 'validator' for n in ast.walk(tree))


async def test_18_manual_end_to_end(manager, req):
    path = Path(__file__).resolve().parents[2] / 'mock_docs/store_sop_manual.md'
    content = await asyncio.to_thread(path.read_text, encoding='utf-8')
    request = req.model_copy(update={'content':content})
    first = await publish(manager, request)
    assert first.parent_count >= 7
    assert any(p.page_number == 3 for p in (await state(manager))[1])
    changed = request.model_copy(update={'content':content.replace('糖浆 25ml', '糖浆 22ml'), 'allowed_roles':['manager']})
    second = await publish(manager, changed)
    assert second.current_version == 2
    doc, parents = await state(manager)
    assert doc.content_hash == manager.compute_content_hash(changed.content)
    assert all(p.version == 2 for p in parents)
    assert any('糖浆 22ml' in p.full_content for p in parents)
    assert all(c.allowed_roles == ['manager'] and c.version == 2 for s in (manager.vector_store, manager.keyword_store) for c in s.data.values())


def test_adapter_protocols():
    assert isinstance(InMemoryVectorStore(), VectorStorePort)
    assert isinstance(InMemoryKeywordStore(), KeywordStorePort)
    assert isinstance(FakeEmbedder(), EmbeddingPort)


async def test_chunking_failure_and_recovery(manager, req, monkeypatch):
    await publish(manager, req)
    original = manager._build_chunks
    def fail(*args):
        raise RuntimeError('chunking failed')
    monkeypatch.setattr(manager, '_build_chunks', fail)
    result = await manager.sync_document(req.model_copy(update={'force_reindex':True}))
    await assert_rollback_without_chunks(manager, result)
    monkeypatch.setattr(manager, '_build_chunks', original)
    assert (await publish(manager, req)).current_version == 2


async def assert_rollback_without_chunks(manager, result):
    assert result.outcome == SyncOutcome.FAILED
    doc, parents = await state(manager)
    assert doc.current_version == 1 and doc.status == DocumentStatus.FAILED
    assert {p.version for p in parents} == {1}


async def test_registration_failure(manager, req, monkeypatch):
    async def fail(*args):
        raise RuntimeError('registration failed')
    monkeypatch.setattr(manager, '_register_or_mark_syncing', fail)
    result = await manager.sync_document(req)
    assert result.outcome == SyncOutcome.FAILED and result.current_version == 0
    assert (await state(manager))[0] is None


async def test_stale_syncing_recovery(manager, req):
    await publish(manager, req)
    async with manager.session_factory.begin() as session:
        await session.execute(update(KBDocument).where(KBDocument.doc_id == req.doc_id).values(status=DocumentStatus.SYNCING))
    assert (await publish(manager, req)).current_version == 2


async def test_cancel_during_write(manager, req):
    await publish(manager, req)
    manager.vector_store.delay = .1
    task = asyncio.create_task(manager.sync_document(req.model_copy(update={'force_reindex':True})))
    while manager.vector_store.in_flight == 0:
        await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(.12)
    assert (await state(manager))[0].current_version == 1
    assert all(c.version == 1 for s in (manager.vector_store, manager.keyword_store) for c in s.data.values())
