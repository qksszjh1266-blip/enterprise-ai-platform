"""异步检索与重排端口（Ports & Adapters）。

- 单元测试注入内存替身；生产注入 Milvus / Elasticsearch / 交叉编码器适配器。
- 查询向量化复用 storage_sync.ports.EmbeddingPort，不在此重复定义。

适配器硬性约束：
1. RetrievalFilter 的三项条件必须作为引擎内 pre-filter 下推（Milvus expr / ES bool.filter），
   严禁先召回再在 Python 侧裁剪权限（会导致 top_k 被无权限数据挤占）。
2. filter.doc_versions 为空时直接返回 []，严禁生成空 IN 表达式退化为全量检索。
3. 返回的候选按相关度降序，rank 从 1 连续递增，长度 <= top_k。
4. 只抛异常，不自行吞错或重试；超时与降级由 HybridRetrievalService 统一控制。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from data_platform.retrieval_engine.models import RetrievalCandidate, RetrievalFilter


@runtime_checkable
class VectorSearchPort(Protocol):
    """稠密向量检索端口（生产：Milvus ANN + 标量过滤）。"""

    async def search(
        self,
        query_vector: Sequence[float],
        retrieval_filter: RetrievalFilter,
        top_k: int,
    ) -> list[RetrievalCandidate]:
        """在 ACL 与有效版本约束内做 ANN 召回。

        返回的每条候选 branch 必须为 RetrievalBranch.VECTOR，
        且 (doc_id, version) 必须命中 retrieval_filter.doc_versions。
        """
        ...


@runtime_checkable
class KeywordSearchPort(Protocol):
    """BM25 关键词检索端口（生产：Elasticsearch match + bool.filter）。"""

    async def search(
        self,
        query: str,
        retrieval_filter: RetrievalFilter,
        top_k: int,
    ) -> list[RetrievalCandidate]:
        """在 ACL 与有效版本约束内做 BM25 召回。

        返回的每条候选 branch 必须为 RetrievalBranch.KEYWORD，
        且 (doc_id, version) 必须命中 retrieval_filter.doc_versions。
        """
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """交叉编码器精排端口（生产：bge-reranker 等内部服务）。"""

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """对 (query, passage) 对逐一打分。

        返回长度必须等于 len(passages)，顺序一一对应；分数越大越相关，量纲不限。
        超长 passage 的截断由适配器负责。passages 为空时返回 []。
        """
        ...
