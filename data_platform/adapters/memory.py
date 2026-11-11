"""Local retrieval adapters over live child payload collections.

Pass the write adapter's ``data`` dictionary directly; subsequent writes and GC
are immediately visible. These deterministic scorers are for local development.
"""
from collections.abc import Mapping, Sequence

from data_platform.retrieval_engine.models import RetrievalBranch, RetrievalCandidate, RetrievalFilter
from data_platform.storage_sync.models import ChildChunkPayload


class InMemoryKeywordSearch:
    def __init__(self, children: Mapping[str, ChildChunkPayload] | Sequence[ChildChunkPayload]):
        self.children = children

    def _search(self, retrieval_filter, top_k, score, branch):
        if not retrieval_filter.doc_versions:
            return []
        children = self.children.values() if isinstance(self.children, Mapping) else self.children
        visible = [c for c in children if c.dept_id == retrieval_filter.dept_id
                   and set(c.allowed_roles).intersection(retrieval_filter.roles)
                   and retrieval_filter.doc_versions.get(c.doc_id) == c.version]
        ranked = sorted(((score(c), c) for c in visible), key=lambda pair: (-pair[0], pair[1].child_id))
        return [RetrievalCandidate(child_id=c.child_id, parent_id=c.parent_id, doc_id=c.doc_id,
            version=c.version, branch=branch, raw_score=value, rank=rank)
            for rank, (value, c) in enumerate(ranked[:top_k], 1)]

    async def search(self, query: str, retrieval_filter: RetrievalFilter, top_k: int) -> list[RetrievalCandidate]:
        return self._search(retrieval_filter, top_k,
            lambda c: sum(c.child_text.count(char) for char in query), RetrievalBranch.KEYWORD)


class InMemoryVectorSearch(InMemoryKeywordSearch):
    async def search(self, query_vector: Sequence[float], retrieval_filter: RetrievalFilter, top_k: int) -> list[RetrievalCandidate]:
        return self._search(retrieval_filter, top_k,
            lambda c: sum(a * b for a, b in zip(query_vector, c.embedding or [], strict=True)), RetrievalBranch.VECTOR)


class InMemoryReranker:
    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(sum(p.count(char) for char in query)) for p in passages]
