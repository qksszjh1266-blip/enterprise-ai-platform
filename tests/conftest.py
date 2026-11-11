import pytest
import asyncio
from contextlib import asynccontextmanager
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from data_platform.storage_sync.models import Base, DocumentIngestRequest
from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.storage_sync.sync_manager import VersionedSyncManager
from tests.fakes import InMemoryVectorStore, InMemoryKeywordStore, FakeEmbedder


@pytest.fixture
async def factory(tmp_path):
    # A cancelled SQLite operation can invalidate the connection; a file keeps
    # the schema and published data intact when StaticPool reconnects.
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "knowledge.sqlite"}', poolclass=StaticPool)
    @event.listens_for(engine.sync_engine, 'connect')
    def configure(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # StaticPool shares one physical connection. SQLite transactions must not
    # overlap on it; serialize DB contexts, while external I/O stays concurrent.
    lock = asyncio.Lock()
    class SingleConnectionFactory:
        @asynccontextmanager
        async def __call__(self):
            async with lock:
                async with maker() as session:
                    yield session

        @asynccontextmanager
        async def begin(self):
            async with lock:
                async with maker.begin() as session:
                    yield session

    yield SingleConnectionFactory()
    await engine.dispose()


@pytest.fixture
async def manager(factory):
    result = VersionedSyncManager(session_factory=factory, chunker=HierarchicalMarkdownChunker(child_max_chars=60, child_overlap_chars=10), vector_store=InMemoryVectorStore(), keyword_store=InMemoryKeywordStore(), embedder=FakeEmbedder())
    yield result
    await result.aclose()


@pytest.fixture
def req():
    return DocumentIngestRequest(doc_id='sop', title='店铺手册', source_system='oa', content='# 店铺\n\n标准作业说明。' * 30, dept_id='ops', allowed_roles=['staff', 'manager'])


@pytest.fixture
async def retrieval(manager):
    from tests.fakes import InMemoryVectorSearch, InMemoryKeywordSearch, FakeReranker
    from data_platform.retrieval_engine.service import HybridRetrievalService
    from data_platform.retrieval_engine.models import RetrievalConfig
    return HybridRetrievalService(session_factory=manager.session_factory, embedder=FakeEmbedder(),
        vector_search=InMemoryVectorSearch(manager.vector_store.data),
        keyword_search=InMemoryKeywordSearch(manager.keyword_store.data), reranker=FakeReranker(),
        config=RetrievalConfig(branch_timeout_s=.05, rerank_timeout_s=.05, embedding_timeout_s=.05))


@pytest.fixture
def search_req():
    from data_platform.retrieval_engine.models import SearchRequest, UserContext
    return SearchRequest(query='店铺', user_context=UserContext(user_id='user', dept_id='ops', roles=['manager']))


@pytest.fixture
async def published(manager, req):
    for doc_id in ('sop', 'other'):
        result = await manager.sync_document(req.model_copy(update={'doc_id': doc_id, 'content': '# 店铺\n\n店铺标准作业说明。'}))
        assert result.outcome.value == 'published'
    await manager.aclose()
