"""异步存储端口（Ports & Adapters）。

使用 typing.Protocol 结构化子类型定义：
- 单元测试注入内存 Mock（无需继承，只需方法签名匹配）；
- 生产环境注入 Milvus / Elasticsearch 适配器（后续在 adapters 中实现）。

所有方法必须为 async，且必须幂等（同一 child_id 重复 upsert 结果一致），
以便同步状态机在失败重试时无副作用。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from data_platform.storage_sync.models import ChildChunkPayload


@runtime_checkable
class VectorStorePort(Protocol):
    """向量存储端口（生产：Milvus）。

    存储字段：child_id(主键), embedding, parent_id, doc_id, version, dept_id, allowed_roles, child_text。
    """

    async def upsert_children(
        self, doc_id: str, version: int, children: Sequence[ChildChunkPayload]
    ) -> int:
        """批量按版本写入子块向量。

        约束：children 中每条的 doc_id/version 必须与入参一致，否则抛 ValueError；
        每条 embedding 不得为 None。返回成功写入条数。
        """
        ...

    async def delete_stale_versions(self, doc_id: str, keep_version: int) -> int:
        """删除 doc_id 下所有 version != keep_version 的子块（旧版本 GC）。返回删除条数。"""
        ...

    async def delete_version(self, doc_id: str, version: int) -> int:
        """删除 doc_id 下指定 version 的子块（影子版本回滚）。返回删除条数。"""
        ...


@runtime_checkable
class KeywordStorePort(Protocol):
    """关键词/BM25 存储端口（生产：Elasticsearch）。

    存储字段：child_id(_id), child_text(分词), parent_id, doc_id, version, dept_id, allowed_roles(keyword)。
    """

    async def upsert_children(
        self, doc_id: str, version: int, children: Sequence[ChildChunkPayload]
    ) -> int:
        """批量按版本写入子块文本索引（bulk index，refresh 策略由适配器决定）。返回成功写入条数。"""
        ...

    async def delete_stale_versions(self, doc_id: str, keep_version: int) -> int:
        """delete_by_query：doc_id == doc_id AND version != keep_version。返回删除条数。"""
        ...

    async def delete_version(self, doc_id: str, version: int) -> int:
        """delete_by_query：doc_id == doc_id AND version == version（影子版本回滚）。返回删除条数。"""
        ...


@runtime_checkable
class EmbeddingPort(Protocol):
    """文本向量化端口（生产：内部 Embedding 服务；单测：确定性假向量）。"""

    @property
    def dimension(self) -> int:
        """向量维度，须与 Milvus collection schema 一致。"""
        ...

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化，返回顺序与 texts 一一对应。"""
        ...
