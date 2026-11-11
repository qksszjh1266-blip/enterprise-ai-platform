"""mcp_server 测试公共夹具与检索替身。"""

from __future__ import annotations

import pytest_asyncio

from data_platform.retrieval_engine.models import (
    DegradedReason, RetrievalBranch, RetrievedParentChunk, SearchRequest, SearchResponse,
)
from mcp_server.identity import CallerPrincipal
from mcp_server.knowledge import build_demo_knowledge_base

STORE_CLERK = CallerPrincipal(user_id="u-clerk", department="store_ops", roles=("clerk",))
STORE_MANAGER = CallerPrincipal(user_id="u-manager", department="store_ops", roles=("clerk", "store_manager"))
FINANCE_STAFF = CallerPrincipal(user_id="u-fin", department="finance", roles=("finance",))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def demo_kb():
    """真实发布链路构建的演示知识库（模块内共享，只读使用）。"""
    knowledge = await build_demo_knowledge_base()
    yield knowledge
    await knowledge.aclose()


def make_chunk(doc_id: str = "doc_a", *, content: str = "正文", section: str = "章节A", page: int | None = 1,
               score: float = 0.5) -> RetrievedParentChunk:
    return RetrievedParentChunk(parent_id=f"{doc_id}:p1", doc_id=doc_id, version=1, section_path=section,
                                full_content=content, page_number=page, score=score, rrf_score=0.1,
                                source_branches=[RetrievalBranch.KEYWORD])


class StubRetrieval:
    """记录请求的检索替身：返回预置结果或抛出预置异常。"""

    def __init__(self, *, results: list[RetrievedParentChunk] | None = None, error: Exception | None = None,
                 degraded_reasons: list[DegradedReason] | None = None) -> None:
        self.results = results or []
        self.error = error
        self.degraded_reasons = degraded_reasons or []
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SearchResponse(query=request.query, results=self.results[:request.top_k],
                              degraded=bool(self.degraded_reasons), degraded_reasons=self.degraded_reasons,
                              latency_ms=1.0)
