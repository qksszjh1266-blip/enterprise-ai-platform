"""存储底座数据模型与传输契约。

本模块包含两类定义：
1. SQLAlchemy 2.0 异步 ORM 表模型（PostgreSQL / SQLite Async）：
   - KBDocument      -> kb_documents      文档台账（含 current_version 蓝绿指针）
   - KBParentChunk   -> kb_parent_chunks  父块原文（按 version 隔离）
2. Pydantic V2 传输契约：
   - DocumentIngestRequest  入库请求
   - ParentChunkSchema      父块传输对象
   - ChildChunkPayload      子块索引载荷（写入 Milvus / ES，携带 ACL 标量过滤字段）
   - ChunkingResult         切分器输出
   - SyncResult             同步状态机输出

兼容性要求：公共类名与模型字段保持稳定，变更需同步调用方与契约测试。
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class DocumentStatus(str, enum.Enum):
    """文档同步状态（蓝绿状态机）。

    PENDING  : 台账已登记，尚无任何已发布版本（current_version == 0）。
    SYNCING  : 正在旁路写入 v+1（影子版本），对外仍服务 current_version。
    ACTIVE   : current_version 已发布且三端（PG/向量/ES）一致。
    FAILED   : 最近一次 v+1 写入失败并已回滚，对外仍服务 current_version。
    DELETED  : 文档已下线（逻辑删除），检索侧必须过滤。
    """

    PENDING = "pending"
    SYNCING = "syncing"
    ACTIVE = "active"
    FAILED = "failed"
    DELETED = "deleted"


class SyncOutcome(str, enum.Enum):
    """单次 sync_document 调用的结果类型。"""

    SKIPPED_UNCHANGED = "skipped_unchanged"  # SHA-256 哈希命中，短路拦截
    PUBLISHED = "published"  # v+1 写入成功并完成 current_version 原子切换
    FAILED = "failed"  # v+1 写入/切换失败，已回滚影子版本


# ---------------------------------------------------------------------------
# SQLAlchemy 2.0 ORM
# ---------------------------------------------------------------------------


class Base(AsyncAttrs, DeclarativeBase):
    """全局声明式基类（异步属性加载支持）。"""

    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }


class KBDocument(Base):
    """文档台账表 kb_documents。

    current_version 是蓝绿发布的唯一权威指针：检索侧只返回
    version == current_version 的子块/父块。
    """

    __tablename__ = "kb_documents"

    doc_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="kb_document_status", native_enum=False, length=16),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    dept_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    allowed_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )


class KBParentChunk(Base):
    """父块原文表 kb_parent_chunks。

    同一 doc_id 的不同 version 父块并存（蓝绿），旧版本由异步 GC 清理。
    """

    __tablename__ = "kb_parent_chunks"
    __table_args__ = (
        Index("ix_kb_parent_chunks_doc_version", "doc_id", "version"),
    )

    parent_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("kb_documents.doc_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    full_content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------------------
# Pydantic V2 传输契约
# ---------------------------------------------------------------------------


class DocumentIngestRequest(BaseModel):
    """文档入库请求（来自 connectors 或 OpenAPI）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    doc_id: str = Field(..., min_length=1, max_length=128, description="业务侧全局唯一文档 ID")
    title: str = Field(..., min_length=1, max_length=512)
    source_system: str = Field(..., min_length=1, max_length=64, description="来源系统，如 feishu / sharepoint / oa")
    content: str = Field(..., description="Markdown 原文（解析器已转换）")
    dept_id: str = Field(..., min_length=1, max_length=64, description="归属部门，ACL 标量过滤字段")
    allowed_roles: list[str] = Field(default_factory=list, description="可见角色列表，ACL 标量过滤字段")
    force_reindex: bool = Field(default=False, description="为 True 时跳过哈希短路，强制生成 v+1")


class ParentChunkSchema(BaseModel):
    """父块传输对象，与 KBParentChunk 一一对应。"""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    parent_id: str = Field(..., max_length=192)
    doc_id: str = Field(..., max_length=128)
    version: int = Field(..., ge=1)
    section_path: str = Field(default="", description="标题层级路径，如 '门店SOP/开店流程/设备检查'")
    full_content: str
    page_number: int | None = Field(default=None, ge=1)


class ChildChunkPayload(BaseModel):
    """子块索引载荷：写入 Milvus（向量）与 Elasticsearch（BM25）。

    parent_id / doc_id / version / dept_id / allowed_roles 为检索期标量过滤字段，
    必须在召回前（pre-filter）参与过滤，严禁召回后再做权限裁剪。
    """

    model_config = ConfigDict(frozen=True)

    child_id: str = Field(..., max_length=224, description="确定性 ID：{parent_id}:c{index}")
    parent_id: str = Field(..., max_length=192)
    doc_id: str = Field(..., max_length=128)
    version: int = Field(..., ge=1)
    dept_id: str = Field(..., max_length=64)
    allowed_roles: list[str] = Field(default_factory=list)
    child_text: str = Field(..., min_length=1)
    chunk_index: int = Field(default=0, ge=0, description="在父块内的序号")
    embedding: list[float] | None = Field(default=None, description="由 EmbeddingPort 回填；ES 侧忽略")


class ChunkingResult(BaseModel):
    """HierarchicalMarkdownChunker 的输出。"""

    model_config = ConfigDict(frozen=True)

    parents: list[ParentChunkSchema] = Field(default_factory=list)
    children: list[ChildChunkPayload] = Field(default_factory=list)


class SyncResult(BaseModel):
    """VersionedSyncManager.sync_document 的结构化返回。

    error_hint 面向 Agent/调用方的结构化修正提示（禁止裸抛 500）。
    """

    doc_id: str
    outcome: SyncOutcome
    previous_version: int = Field(..., ge=0)
    current_version: int = Field(..., ge=0, description="调用结束后对外服务的版本")
    content_hash: str
    parent_count: int = Field(default=0, ge=0)
    child_count: int = Field(default=0, ge=0)
    error_hint: str | None = None
