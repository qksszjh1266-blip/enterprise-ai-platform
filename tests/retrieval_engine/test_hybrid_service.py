import asyncio
import pytest
from sqlalchemy import update, delete
from data_platform.storage_sync.models import KBDocument, KBParentChunk, DocumentStatus
from data_platform.retrieval_engine.models import RetrievalCandidate, RetrievalBranch as B, DegradedReason as D, RetrievalFilter
from data_platform.retrieval_engine.errors import RetrievalUnavailableError, LedgerUnavailableError, FilterTooBroadError
from data_platform.retrieval_engine.ports import VectorSearchPort, KeywordSearchPort, RerankerPort


async def test_01_normal(retrieval, published, search_req):
    response = await retrieval.search(search_req)
    assert not response.degraded and len(response.results) == 2
    assert all(p.full_content == '门店\n门店标准作业说明。' and p.section_path and p.source_branches == [B.VECTOR, B.KEYWORD] for p in response.results)


async def test_02_shadow(retrieval, manager, published, req, search_req, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    switch = manager._switch_current_version
    async def pause(*args):
        entered.set()
        await release.wait()
        return await switch(*args)
    monkeypatch.setattr(manager, '_switch_current_version', pause)
    task = asyncio.create_task(manager.sync_document(req.model_copy(update={'force_reindex': True})))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert any(c.version == 2 for c in manager.vector_store.data.values())
        response = await retrieval.search(search_req)
        assert response.results and all(p.version == 1 for p in response.results)
    finally:
        release.set()
        await task


async def test_03_failed_serves(retrieval, manager, published, req, search_req):
    manager.vector_store.fail_on.add('upsert_children')
    assert (await manager.sync_document(req)).outcome.value == 'failed'
    assert 'sop' in {p.doc_id for p in (await retrieval.search(search_req)).results}


async def test_04_syncing(retrieval, factory, published, search_req):
    async with factory.begin() as s:
        await s.execute(update(KBDocument).values(status=DocumentStatus.SYNCING))
    assert len((await retrieval.search(search_req)).results) == 2


@pytest.mark.parametrize('values', [{'status': DocumentStatus.DELETED}, {'status': DocumentStatus.PENDING}, {'current_version': 0}])
async def test_05_invisible(retrieval, factory, published, search_req, values):
    async with factory.begin() as s:
        await s.execute(update(KBDocument).values(**values))
    assert not (await retrieval.search(search_req)).results


async def test_06_department(retrieval, published, search_req):
    user = search_req.user_context.model_copy(update={'dept_id': 'finance'})
    assert not (await retrieval.search(search_req.model_copy(update={'user_context': user}))).results
    assert not retrieval.vector_search.calls and not retrieval.keyword_search.calls


async def test_07_roles(retrieval, factory, published, search_req):
    async with factory.begin() as s:
        await s.execute(update(KBDocument).values(allowed_roles=['manager']))
    for roles, visible in [(['staff'], False), (['staff', 'manager'], True)]:
        request = search_req.model_copy(update={'user_context': search_req.user_context.model_copy(update={'roles': roles})})
        assert bool((await retrieval.search(request)).results) is visible


async def test_08_empty_acl(retrieval, factory, published, search_req):
    async with factory.begin() as s:
        await s.execute(update(KBDocument).values(allowed_roles=[]))
    assert not (await retrieval.search(search_req)).results


async def test_09_empty_short_circuit(retrieval, search_req):
    result = await retrieval.search(search_req)
    assert not result.results and not result.degraded
    assert not retrieval.vector_search.calls and not retrieval.keyword_search.calls and not retrieval.reranker.calls


async def test_10_prefilter(retrieval, published, search_req):
    await retrieval.search(search_req)
    expected = RetrievalFilter(dept_id='ops', roles=['manager'], doc_versions={'sop': 1, 'other': 1})
    assert retrieval.vector_search.calls == retrieval.keyword_search.calls == [expected]


def candidate(parent='p', doc='sop', version=1, rank=1, branch=B.VECTOR):
    return RetrievalCandidate(child_id=f'{parent}:c{rank}', parent_id=parent, doc_id=doc, version=version, branch=branch, raw_score=1, rank=rank)


async def test_11_leaks(retrieval, published, search_req, caplog):
    retrieval.vector_search.leak = [candidate(doc='secret'), candidate(version=2)]
    result = await retrieval.search(search_req)
    assert len(result.results) == 2 and not result.degraded
    assert '过滤丢弃 2' in caplog.text


@pytest.mark.parametrize('branch,attribute,value,reason', [
    ('keyword_search','delay',.2,D.KEYWORD_TIMEOUT),
    ('keyword_search','fail',RuntimeError(),D.KEYWORD_ERROR),
    ('embedder','fail',True,D.EMBEDDING_FAILED),
    ('embedder','delay',.2,D.EMBEDDING_FAILED),
    ('vector_search','fail',RuntimeError(),D.VECTOR_ERROR),
    ('vector_search','delay',.2,D.VECTOR_TIMEOUT),
])
async def test_12_to_15_degradation(retrieval, published, search_req, branch, attribute, value, reason):
    setattr(getattr(retrieval, branch), attribute, value)
    result = await retrieval.search(search_req)
    assert result.results and result.degraded and result.degraded_reasons == [reason] and result.degraded_reason
    expected = B.VECTOR if branch == 'keyword_search' else B.KEYWORD
    assert all(p.source_branches == [expected] for p in result.results)


async def test_16_both_fail(retrieval, published, search_req):
    retrieval.vector_search.fail = retrieval.keyword_search.fail = RuntimeError()
    with pytest.raises(RetrievalUnavailableError):
        await retrieval.search(search_req)


async def test_17_concurrency(retrieval, published, search_req):
    retrieval.config = retrieval.config.model_copy(update={'branch_timeout_s': 1})
    retrieval.vector_search.delay = retrieval.keyword_search.delay = .2
    assert (await retrieval.search(search_req)).latency_ms < 350


def test_18_rrf(retrieval):
    vec = [candidate('b'), candidate('b', rank=2), candidate('a', rank=3)]
    kw = [candidate('a', branch=B.KEYWORD), candidate('b', rank=2, branch=B.KEYWORD)]
    fused = retrieval._fuse_rrf(vec, kw, 60)
    assert [p.parent_id for p in fused] == ['a', 'b']
    assert all(p.rrf_score == 1/61 + 1/62 and p.source_branches == [B.VECTOR, B.KEYWORD] for p in fused)
    assert fused == retrieval._fuse_rrf(vec, kw, 60)


async def test_19_dedup(retrieval, manager, req, search_req):
    await manager.sync_document(req.model_copy(update={'content': '# 门店\n\n' + '标准操作。' * 100}))
    result = await retrieval.search(search_req)
    assert len(manager.vector_store.data) > len(result.results)
    assert len(result.results) == len({p.parent_id for p in result.results})


async def test_20_rerank(retrieval, published, search_req, monkeypatch):
    baseline = await retrieval.search(search_req.model_copy(update={'enable_rerank': False}))
    async def reverse(query, passages):
        return list(range(len(passages)))
    monkeypatch.setattr(retrieval.reranker, 'rerank', reverse)
    result = await retrieval.search(search_req)
    assert [p.parent_id for p in result.results] == [p.parent_id for p in reversed(baseline.results)]
    assert all(p.score == p.rerank_score for p in result.results)


@pytest.mark.parametrize('attribute,value,reason', [('delay',.2,D.RERANK_TIMEOUT), ('fail',RuntimeError(),D.RERANK_ERROR), ('wrong_length',True,D.RERANK_ERROR)])
async def test_21_rerank_fallback(retrieval, published, search_req, attribute, value, reason):
    baseline = await retrieval.search(search_req.model_copy(update={'enable_rerank': False}))
    setattr(retrieval.reranker, attribute, value)
    result = await retrieval.search(search_req)
    assert result.results == baseline.results and result.degraded_reasons == [reason]
    assert all(p.rerank_score is None for p in result.results)


async def test_22_disable_rerank(retrieval, published, search_req):
    result = await retrieval.search(search_req.model_copy(update={'enable_rerank': False}))
    assert not result.degraded and not retrieval.reranker.calls


async def test_23_limits(retrieval, published, search_req):
    retrieval.config = retrieval.config.model_copy(update={'rerank_candidates': 1})
    result = await retrieval.search(search_req.model_copy(update={'top_k': 1}))
    assert len(result.results) == 1 and len(retrieval.reranker.calls[0][1]) == 1


@pytest.mark.parametrize('mutation', ['delete', 'version', 'doc_id'])
async def test_24_missing(retrieval, published, factory, search_req, monkeypatch, mutation):
    fetch = retrieval._fetch_parents
    async def remove(fused):
        async with factory.begin() as s:
            statement = delete(KBParentChunk) if mutation == 'delete' else update(KBParentChunk).values(**{mutation: 99 if mutation == 'version' else ('sop' if fused[0].doc_id == 'other' else 'other')})
            await s.execute(statement.where(KBParentChunk.parent_id == fused[0].parent_id))
        return await fetch(fused)
    monkeypatch.setattr(retrieval, '_fetch_parents', remove)
    result = await retrieval.search(search_req)
    assert len(result.results) == 1 and result.degraded_reasons == [D.PARENT_MISSING]


async def test_25_ledger_failure(retrieval, search_req):
    def broken():
        raise RuntimeError('db offline')
    retrieval.session_factory = broken
    with pytest.raises(LedgerUnavailableError):
        await retrieval.search(search_req)
    assert not retrieval.vector_search.calls and not retrieval.keyword_search.calls


async def test_26_broad(retrieval, published, search_req):
    retrieval.config = retrieval.config.model_copy(update={'max_filter_docs': 1})
    with pytest.raises(FilterTooBroadError):
        await retrieval.search(search_req)
    assert not retrieval.vector_search.calls


async def test_27_statistics(retrieval, published, search_req):
    result = await retrieval.search(search_req)
    assert result.latency_ms > 0
    assert {'filter','embedding','vector','keyword','fusion','fetch','rerank'} <= result.stage_latency_ms.keys()
    assert all(t >= 0 for t in result.stage_latency_ms.values())
    assert result.candidate_counts == {'vector': 2, 'keyword': 2, 'fused': 2}


def test_protocols(retrieval):
    assert isinstance(retrieval.vector_search, VectorSearchPort)
    assert isinstance(retrieval.keyword_search, KeywordSearchPort)
    assert isinstance(retrieval.reranker, RerankerPort)


async def test_cancel_propagates(retrieval, published, search_req):
    retrieval.keyword_search.fail = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await retrieval.search(search_req)


@pytest.mark.parametrize('adapter_name', ['vector_search', 'keyword_search'])
async def test_adapter_prefilters_before_top_k(retrieval, manager, published, adapter_name):
    adapter = getattr(retrieval, adapter_name)
    sample = next(iter(adapter.children.values()))
    # Every decoy would outrank valid data if filtering happened after top_k.
    for index, changes in enumerate([{'dept_id': 'secret'}, {'allowed_roles': []},
            {'allowed_roles': ['finance']}, {'version': 2}, {'doc_id': 'secret'}]):
        adapter.children[f'decoy{index}'] = sample.model_copy(update={
            'child_id': f'decoy{index}', 'child_text': '门店' * 1000, 'embedding': [1000.] * 8, **changes})
    query = [1.] * 8 if adapter_name == 'vector_search' else '门店'
    filt = RetrievalFilter(dept_id='ops', roles=['manager'], doc_versions={'sop': 1, 'other': 1})
    result = await adapter.search(query, filt, 1)
    assert len(result) == 1 and not result[0].child_id.startswith('decoy') and result[0].rank == 1
    assert not await adapter.search(query, filt.model_copy(update={'doc_versions': {}}), 10)


async def test_fetch_database_failure(retrieval, published, search_req, monkeypatch):
    original = retrieval._build_filter
    async def build(user):
        filt = await original(user)
        def broken():
            raise RuntimeError('database disconnected')
        retrieval.session_factory = broken
        return filt
    monkeypatch.setattr(retrieval, '_build_filter', build)
    with pytest.raises(RetrievalUnavailableError):
        await retrieval.search(search_req)


async def test_concurrent_request_metrics_isolated(retrieval, published, search_req):
    empty = search_req.model_copy(update={'user_context': search_req.user_context.model_copy(update={'dept_id': 'secret'})})
    full, none = await asyncio.gather(retrieval.search(search_req), retrieval.search(empty))
    assert full.results and 'vector' in full.stage_latency_ms
    assert not none.results and set(none.stage_latency_ms) == {'filter'}
