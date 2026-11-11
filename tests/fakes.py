"""Deterministic async adapters with partial writes and I/O fault injection."""
import asyncio
import hashlib
from collections import Counter
from collections.abc import Sequence
from data_platform.storage_sync.models import ChildChunkPayload


class InMemoryKeywordStore:
    def __init__(self, *, fail_on=None, fail_after_n=None, delay=0):
        self.data: dict[str, ChildChunkPayload] = {}
        self.fail_on = set(fail_on or ())
        self.fail_after_n = fail_after_n
        self.delay = delay
        self.calls = Counter()
        self.in_flight = 0
        self.max_in_flight = 0

    async def _before(self, method):
        self.calls[method] += 1
        await asyncio.sleep(self.delay)
        if method in self.fail_on:
            raise RuntimeError(f"injected {method}")

    async def upsert_children(self, doc_id: str, version: int, children: Sequence[ChildChunkPayload]) -> int:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self._before('upsert_children')
            if any(c.doc_id != doc_id or c.version != version for c in children):
                raise ValueError('document/version mismatch')
            for n, child in enumerate(children):
                if self.fail_after_n is not None and n >= self.fail_after_n:
                    raise RuntimeError('injected partial write')
                self.data[child.child_id] = child.model_copy(deep=True)
            return len(children)
        finally:
            self.in_flight -= 1

    async def delete_version(self, doc_id: str, version: int) -> int:
        await self._before('delete_version')
        keys = [k for k, c in self.data.items() if c.doc_id == doc_id and c.version == version]
        for k in keys:
            del self.data[k]
        return len(keys)

    async def delete_stale_versions(self, doc_id: str, keep_version: int) -> int:
        await self._before('delete_stale_versions')
        keys = [k for k, c in self.data.items() if c.doc_id == doc_id and c.version != keep_version]
        for k in keys:
            del self.data[k]
        return len(keys)


class InMemoryVectorStore(InMemoryKeywordStore):
    async def upsert_children(self, doc_id: str, version: int, children: Sequence[ChildChunkPayload]) -> int:
        if any(c.embedding is None for c in children):
            raise ValueError('missing embedding')
        return await super().upsert_children(doc_id, version, children)


class FakeEmbedder:
    dimension = 8

    def __init__(self, *, wrong_dimension=False, delay=0, fail=False):
        self.wrong_dimension = wrong_dimension
        self.delay = delay
        self.fail = fail
        self.calls = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError('embedding unavailable')
        return [[b / 255 for b in hashlib.sha256(t.encode()).digest()[:7 if self.wrong_dimension else 8]] for t in texts]


from data_platform.adapters.memory import (
    InMemoryVectorSearch as LocalVectorSearch,
    InMemoryKeywordSearch as LocalKeywordSearch,
    InMemoryReranker,
)


class SearchFaults:
    def __init__(self, children=(), *, delay=0, fail=None, leak=()):
        super().__init__(children)
        self.delay, self.fail, self.leak = delay, fail, list(leak)
        self.calls = []

    async def search(self, query, retrieval_filter, top_k):
        self.calls.append(retrieval_filter)
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return await super().search(query, retrieval_filter, top_k) + self.leak


class InMemoryVectorSearch(SearchFaults, LocalVectorSearch):
    pass


class InMemoryKeywordSearch(SearchFaults, LocalKeywordSearch):
    pass


class FakeReranker(InMemoryReranker):
    def __init__(self, *, delay=0, fail=None, wrong_length=False):
        self.delay, self.fail, self.wrong_length = delay, fail, wrong_length
        self.calls = []

    async def rerank(self, query, passages):
        self.calls.append((query, list(passages)))
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        scores = await super().rerank(query, passages)
        return scores + [0] if self.wrong_length else scores
