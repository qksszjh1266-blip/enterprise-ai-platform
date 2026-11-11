import asyncio
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport
from data_platform.mcp_server.app import create_app
from data_platform.retrieval_engine.models import SearchResponse, ToolErrorResponse
from data_platform.retrieval_engine.errors import LedgerUnavailableError
from data_platform.storage_sync.errors import ShadowWriteError


@pytest.fixture
async def client(retrieval, manager):
    app = create_app(retrieval_service=retrieval, sync_manager=manager)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
            yield client


async def search(client, req):
    return await client.post('/api/v1/knowledge/search', json=req.model_dump())


def error(response, status, code):
    assert response.status_code == status, response.text
    body = ToolErrorResponse.model_validate(response.json())
    assert body.error_code == code and body.hint
    return body


def test_01_openapi():
    schema = create_app().openapi()
    assert {p['post']['operationId'] for p in schema['paths'].values()} == {'ingest_knowledge_document','search_knowledge_base'}
    assert {'SearchRequest','SearchResponse','ToolErrorResponse','DocumentIngestRequest','SyncResult'} <= schema['components']['schemas'].keys()


async def test_02_search(client, published, search_req):
    response = await search(client, search_req)
    assert response.status_code == 200
    assert SearchResponse.model_validate(response.json()).results


async def test_03_degraded(client, retrieval, published, search_req):
    retrieval.keyword_search.delay = .2
    response = await search(client, search_req)
    assert response.status_code == 200
    assert SearchResponse.model_validate(response.json()).degraded


async def test_04_unavailable(client, retrieval, published, search_req):
    retrieval.vector_search.fail = retrieval.keyword_search.fail = RuntimeError()
    assert error(await search(client, search_req), 503, 'RETRIEVAL_UNAVAILABLE').retryable


@pytest.mark.parametrize('mutation,loc', [('top_k','body.top_k'), ('missing','body.user_context'), ('roles','body.user_context.roles'), ('extra','body.extra')])
async def test_05_validation(client, search_req, mutation, loc):
    body = search_req.model_dump()
    if mutation == 'top_k': body['top_k'] = 0
    elif mutation == 'missing': del body['user_context']
    elif mutation == 'roles': body['user_context']['roles'] = []
    else: body['extra'] = True
    result = error(await client.post('/api/v1/knowledge/search', json=body), 422, 'INVALID_ARGUMENT')
    assert loc in [d['loc'] for d in result.details] and loc in result.hint


async def test_06_ingest(client, req):
    response = await client.post('/api/v1/knowledge/ingest', json=req.model_dump())
    assert response.status_code == 200 and response.json()['outcome'] == 'published'


async def test_07_ingest_failure(client, manager, req):
    manager.vector_store.fail_on.add('upsert_children')
    response = await client.post('/api/v1/knowledge/ingest', json=req.model_dump())
    assert response.status_code == 200 and response.json()['outcome'] == 'failed'
    assert response.json()['error_hint'].startswith('[SHADOW_WRITE_FAILED]')


async def test_08_uninitialized(search_req, req):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url='http://test') as client:
        error(await search(client, search_req), 503, 'RETRIEVAL_UNAVAILABLE')
        error(await client.post('/api/v1/knowledge/ingest', json=req.model_dump()), 503, 'RETRIEVAL_UNAVAILABLE')


async def test_09_unexpected(client, retrieval, search_req, monkeypatch):
    async def boom(body):
        raise RuntimeError('boom /secret/path')
    monkeypatch.setattr(retrieval, 'search', boom)
    response = await search(client, search_req)
    error(response, 503, 'INTERNAL_ERROR')
    assert all(s not in response.text for s in ('boom','/secret/path','Traceback','RuntimeError'))


def test_10_v2_scan():
    for path in Path('data_platform').rglob('*.py'):
        assert '.dict()' not in path.read_text() and '@validator' not in path.read_text()


async def test_manual_route_end_to_end(client, retrieval, req, search_req):
    content = await asyncio.to_thread(Path('mock_docs/store_sop_manual.md').read_text, encoding='utf-8')
    body = req.model_copy(update={'content': content, 'allowed_roles': ['manager']})
    response = await client.post('/api/v1/knowledge/ingest', json=body.model_dump())
    assert response.status_code == 200 and response.json()['outcome'] == 'published'
    allowed = SearchResponse.model_validate((await search(client, search_req)).json())
    assert allowed.results and all(p.doc_id == 'sop' for p in allowed.results)
    denied = search_req.model_copy(update={'user_context': search_req.user_context.model_copy(update={'roles': ['staff']})})
    assert not SearchResponse.model_validate((await search(client, denied)).json()).results
    retrieval.keyword_search.fail = RuntimeError('offline')
    response = await search(client, search_req)
    degraded = SearchResponse.model_validate(response.json())
    assert response.status_code == 200 and degraded.results and degraded.degraded_reasons == ['keyword_error']
    assert all(p.source_branches == ['vector'] and p.version == 1 for p in degraded.results)
    assert not SearchResponse.model_validate((await search(client, denied)).json()).results


async def test_error_mappings(client, retrieval, manager, published, search_req, req, monkeypatch):
    retrieval.config = retrieval.config.model_copy(update={'max_filter_docs': 1})
    error(await search(client, search_req), 400, 'FILTER_TOO_BROAD')
    async def ledger(body):
        raise LedgerUnavailableError('台账不可用')
    monkeypatch.setattr(retrieval, 'search', ledger)
    error(await search(client, search_req), 503, 'LEDGER_UNAVAILABLE')
    async def storage(body):
        raise ShadowWriteError('写入失败', doc_id='sop', version=2)
    monkeypatch.setattr(manager, 'sync_document', storage)
    error(await client.post('/api/v1/knowledge/ingest', json=req.model_dump()), 503, 'SHADOW_WRITE_FAILED')


async def test_lifespan_closes(manager, monkeypatch):
    calls = []
    async def close(): calls.append(True)
    monkeypatch.setattr(manager, 'aclose', close)
    app = create_app(sync_manager=manager)
    async with app.router.lifespan_context(app):
        assert not calls
    assert calls == [True]
