"""本地演示知识库装配（stdio 启动时使用）。

复用 web_console 的演示语料、内存索引与离线检索适配器，经 VersionedSyncManager 正式发布入库，
再装配原有的 HybridRetrievalService。与工作台运行时的区别：只装配知识检索链路，
不创建智能体、业务沙箱或任何出网 HTTP 客户端。

生产环境不使用本模块：由宿主注入接入 Milvus / Elasticsearch 的 HybridRetrievalService 实例。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from data_platform.adapters.memory import InMemoryVectorSearch
from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.retrieval_engine.service import HybridRetrievalService
from data_platform.storage_sync.models import Base, DocumentIngestRequest, SyncOutcome
from data_platform.storage_sync.sync_manager import VersionedSyncManager
from web_console.adapters import BigramKeywordSearch, BigramReranker, LocalHashEmbedder
from web_console.runtime import MemoryIndex, _corpus


@dataclass
class DemoKnowledgeBase:
    """已发布的本地演示知识库及其检索服务。"""

    workdir: tempfile.TemporaryDirectory
    engine: AsyncEngine
    retrieval: HybridRetrievalService
    documents: int

    async def aclose(self) -> None:
        await self.engine.dispose()
        self.workdir.cleanup()


async def build_demo_knowledge_base() -> DemoKnowledgeBase:
    """发布演示语料（部门 / 角色划分与工作台、评测语料一致）并返回检索服务。"""
    workdir = tempfile.TemporaryDirectory(prefix="enterprise-mcp-")
    engine = create_async_engine(f"sqlite+aiosqlite:///{Path(workdir.name) / 'knowledge.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        embedder, vector_index, keyword_index = LocalHashEmbedder(), MemoryIndex(), MemoryIndex()
        manager = VersionedSyncManager(
            session_factory=factory,
            chunker=HierarchicalMarkdownChunker(child_max_chars=120, child_overlap_chars=20),
            vector_store=vector_index, keyword_store=keyword_index, embedder=embedder)
        corpus = _corpus()
        try:
            for doc_id, title, content, dept, roles in corpus:
                result = await manager.sync_document(DocumentIngestRequest(
                    doc_id=doc_id, title=title, source_system="mock_docs",
                    content=content, dept_id=dept, allowed_roles=roles))
                if result.outcome is not SyncOutcome.PUBLISHED:
                    raise RuntimeError(f"演示知识库发布失败：{doc_id}")
        finally:
            await manager.aclose()
        retrieval = HybridRetrievalService(
            session_factory=factory, embedder=embedder,
            vector_search=InMemoryVectorSearch(vector_index.data),
            keyword_search=BigramKeywordSearch(keyword_index.data),
            reranker=BigramReranker())
    except BaseException:
        await engine.dispose()
        workdir.cleanup()
        raise
    return DemoKnowledgeBase(workdir=workdir, engine=engine, retrieval=retrieval, documents=len(corpus))
