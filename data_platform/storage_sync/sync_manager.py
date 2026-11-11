"""蓝绿版本同步状态机（骨架）。

状态流转（以 current_version = v 为例）：

    sync_document(request)
      │
      ├─ ① SHA-256 哈希短路：content_hash 未变且 status == ACTIVE 且非 force_reindex
      │      └─> 返回 SKIPPED_UNCHANGED（零写入）
      │
      ├─ ② 登记台账：新文档插入 (current_version=0, PENDING)；status -> SYNCING
      │
      ├─ ③ 父子切分：HierarchicalMarkdownChunker.chunk(version = v+1)
      │
      ├─ ④ 向量化：EmbeddingPort.embed_texts 回填 child.embedding
      │
      ├─ ⑤ 旁路并发写入 v+1（asyncio.gather）：
      │      PG kb_parent_chunks ‖ VectorStorePort.upsert_children ‖ KeywordStorePort.upsert_children
      │      任一失败 -> 回滚 v+1 三端数据 -> status=FAILED -> 返回 FAILED（对外仍服务 v）
      │
      ├─ ⑥ 原子切换：UPDATE kb_documents SET current_version=v+1, content_hash, status=ACTIVE
      │               WHERE doc_id=? AND current_version=v   （乐观锁 CAS）
      │      rowcount != 1 -> VersionConflictError -> 回滚 v+1
      │
      └─ ⑦ 异步 GC：后台任务清理三端 version != v+1 的旧数据（失败只记日志，不影响发布）

检索侧不变量：任何时刻只返回 version == kb_documents.current_version 的数据，
因此 ⑤ 期间写入的影子版本对外不可见，⑦ 之前残留的旧版本也不可见。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from sqlalchemy import delete, update
from data_platform.storage_sync.errors import StorageSyncError, ShadowWriteError, RollbackError, VersionConflictError, GarbageCollectionError
from data_platform.storage_sync.models import KBParentChunk, DocumentStatus, SyncOutcome
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.storage_sync.models import (
    ChildChunkPayload,
    ChunkingResult,
    DocumentIngestRequest,
    KBDocument,
    ParentChunkSchema,
    SyncResult,
)
from data_platform.storage_sync.ports import EmbeddingPort, KeywordStorePort, VectorStorePort


class VersionedSyncManager:
    """蓝绿版本同步管理器：保证 PG / Milvus / ES 三端对外一致、无脏读。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        chunker: HierarchicalMarkdownChunker,
        vector_store: VectorStorePort,
        keyword_store: KeywordStorePort,
        embedder: EmbeddingPort,
        embed_batch_size: int = 64,
        gc_enabled: bool = True,
    ) -> None:
        """注入依赖并初始化内部状态。

        内部状态：
            self._doc_locks: dict[str, asyncio.Lock]   同进程内按 doc_id 串行化
            self._gc_tasks: set[asyncio.Task[None]]    持有 GC 任务强引用，防止被回收
        """
        if embed_batch_size < 1:
            raise ValueError("embed_batch_size must be positive")
        self.session_factory = session_factory
        self.chunker = chunker
        self.vector_store = vector_store
        self.keyword_store = keyword_store
        self.embedder = embedder
        self.embed_batch_size = embed_batch_size
        self.gc_enabled = gc_enabled
        self._doc_locks: dict[str, asyncio.Lock] = {}
        self._gc_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def sync_document(self, request: DocumentIngestRequest) -> SyncResult:
        """同步状态机主入口（编排 ①~⑦，见模块文档）。

        - 同一 doc_id 在进程内经 _doc_locks 串行；跨进程依赖 ⑥ 的 CAS。
        - StorageSyncError 子类一律捕获并转为 SyncResult(outcome=FAILED, error_hint=...)，不向上抛出。
        - 非预期异常同样回滚并转为 FAILED，error_hint 给出通用重试建议。
        """
        content_hash = self.compute_content_hash(request.content)
        async with self._doc_locks.setdefault(request.doc_id, asyncio.Lock()):
            v = 0
            registered = False
            shadow_started = False
            chunks = ChunkingResult()
            try:
                async with self.session_factory() as session:
                    existing = await self._load_document(session, request.doc_id)
                    v = existing.current_version if existing else 0
                    if self._should_skip(existing, content_hash, request):
                        return SyncResult(doc_id=request.doc_id, outcome=SyncOutcome.SKIPPED_UNCHANGED, previous_version=v, current_version=v, content_hash=content_hash)
                v = await self._register_or_mark_syncing(request, existing)
                registered = True
                chunks = await asyncio.to_thread(self._build_chunks, request, v+1)
                children = await self._embed_children(chunks.children)
                chunks = chunks.model_copy(update={"children": children})
                shadow_started = True
                await self._write_shadow_version(request.doc_id, v+1, chunks)
                await self._switch_current_version(request.doc_id, v, v+1, content_hash)
            except BaseException as exc:
                if not isinstance(exc, (Exception, asyncio.CancelledError)):
                    raise
                try:
                    if shadow_started and not isinstance(exc, (ShadowWriteError, RollbackError)):
                        await self._rollback_shadow_version(request.doc_id, v+1)
                    elif registered and not shadow_started:
                        await self._mark_failed(request.doc_id, v)
                except Exception as rollback_exc:
                    exc = RollbackError(f"{exc}; rollback: {rollback_exc}", doc_id=request.doc_id, version=v+1)
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                # A cancellation must propagate even if cleanup itself failed.
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                current = v
                try:
                    async with self.session_factory() as session:
                        latest = await self._load_document(session, request.doc_id)
                        if latest:
                            current = latest.current_version
                except Exception:
                    pass
                error = exc if isinstance(exc, StorageSyncError) else StorageSyncError(f"同步失败: {exc}", doc_id=request.doc_id, version=v+1)
                return SyncResult(doc_id=request.doc_id, outcome=SyncOutcome.FAILED, previous_version=v, current_version=current, content_hash=content_hash, error_hint=error.to_hint())
            self._schedule_gc(request.doc_id, v+1)
            return SyncResult(doc_id=request.doc_id, outcome=SyncOutcome.PUBLISHED, previous_version=v, current_version=v+1, content_hash=content_hash, parent_count=len(chunks.parents), child_count=len(chunks.children))

    async def aclose(self) -> None:
        """优雅关闭：等待（或超时取消）所有挂起的 GC 任务。"""
        if self._gc_tasks:
            _, pending = await asyncio.wait(tuple(self._gc_tasks), timeout=30)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # ① 哈希短路
    # ------------------------------------------------------------------

    @staticmethod
    def compute_content_hash(content: str) -> str:
        """计算 SHA-256 十六进制摘要（UTF-8 编码；先统一换行符为 '\\n' 并去除首尾空白）。"""
        return hashlib.sha256(content.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()

    def _should_skip(
        self, existing: KBDocument | None, content_hash: str, request: DocumentIngestRequest
    ) -> bool:
        """判断是否命中短路：existing 存在 且 hash 相同 且 status == ACTIVE 且 not force_reindex。

        注意：仅 ACL 字段（dept_id / allowed_roles）变化而正文未变时，不得短路
        （ACL 冗余存储在子块中，必须重建 v+1）。
        """
        return bool(existing and existing.content_hash == content_hash and existing.status == DocumentStatus.ACTIVE and not request.force_reindex and existing.dept_id == request.dept_id and sorted(existing.allowed_roles) == sorted(request.allowed_roles))

    # ------------------------------------------------------------------
    # ② 台账登记
    # ------------------------------------------------------------------

    async def _load_document(self, session: AsyncSession, doc_id: str) -> KBDocument | None:
        """按主键读取台账行。"""
        return await session.get(KBDocument, doc_id)

    async def _register_or_mark_syncing(
        self, request: DocumentIngestRequest, existing: KBDocument | None
    ) -> int:
        """新文档插入台账(current_version=0, PENDING)；已存在则更新 title/ACL 元数据并置 SYNCING。

        返回进入本次同步前的 current_version（即 v）。独立短事务提交。
        """
        v = existing.current_version if existing else 0
        values = dict(title=request.title, source_system=request.source_system, dept_id=request.dept_id, allowed_roles=list(request.allowed_roles), status=DocumentStatus.SYNCING)
        async with self.session_factory.begin() as session:
            if existing is None:
                session.add(KBDocument(doc_id=request.doc_id, current_version=0, **values))
            else:
                await session.execute(update(KBDocument).where(KBDocument.doc_id == request.doc_id, KBDocument.current_version == v).values(**values))
        return v

    # ------------------------------------------------------------------
    # ③④ 切分与向量化
    # ------------------------------------------------------------------

    def _build_chunks(self, request: DocumentIngestRequest, next_version: int) -> ChunkingResult:
        """调用 chunker.chunk 生成 v+1 的父子块（CPU 密集，可经 asyncio.to_thread 调用）。"""
        return self.chunker.chunk(doc_id=request.doc_id, version=next_version, markdown=request.content, dept_id=request.dept_id, allowed_roles=request.allowed_roles)

    async def _embed_children(
        self, children: Sequence[ChildChunkPayload]
    ) -> list[ChildChunkPayload]:
        """按 embed_batch_size 分批，asyncio.gather 并发向量化，返回回填 embedding 的新子块列表
        （模型 frozen，使用 model_copy(update=...)）。维度与 embedder.dimension 不符抛 ShadowWriteError。
        """
        batches = [children[i:i+self.embed_batch_size] for i in range(0, len(children), self.embed_batch_size)]
        results = await asyncio.gather(*(self.embedder.embed_texts([c.child_text for c in batch]) for batch in batches), return_exceptions=True)
        output = []
        for batch, vectors in zip(batches, results):
            if isinstance(vectors, BaseException):
                raise vectors
            if len(vectors) != len(batch) or any(len(vec) != self.embedder.dimension for vec in vectors):
                raise ShadowWriteError("Embedding dimension or batch cardinality mismatch")
            output.extend(c.model_copy(update={"embedding": vec}) for c, vec in zip(batch, vectors))
        return output

    # ------------------------------------------------------------------
    # ⑤ 旁路并发写入
    # ------------------------------------------------------------------

    async def _write_shadow_version(
        self, doc_id: str, next_version: int, chunks: ChunkingResult
    ) -> None:
        """asyncio.gather(return_exceptions=True) 并发三端写入 v+1。

        任一结果为异常 -> 调用 _rollback_shadow_version -> 抛 ShadowWriteError（链式 from 首个异常）。
        """
        results = await asyncio.gather(self._write_parent_chunks(doc_id, next_version, chunks.parents), self._write_vector_children(doc_id, next_version, chunks.children), self._write_keyword_children(doc_id, next_version, chunks.children), return_exceptions=True)
        first = next((r for r in results if isinstance(r, BaseException)), None)
        if first is not None:
            try:
                await self._rollback_shadow_version(doc_id, next_version)
            except RollbackError as exc:
                raise RollbackError(f"write: {first}; rollback: {exc}", doc_id=doc_id, version=next_version) from first
            raise ShadowWriteError(str(first), doc_id=doc_id, version=next_version) from first

    async def _write_parent_chunks(
        self, doc_id: str, version: int, parents: Sequence[ParentChunkSchema]
    ) -> int:
        """在独立事务中批量插入 KBParentChunk（先删除同 doc_id+version 残留以保证幂等）。返回条数。"""
        async with self.session_factory.begin() as session:
            await session.execute(delete(KBParentChunk).where(KBParentChunk.doc_id == doc_id, KBParentChunk.version == version))
            session.add_all([KBParentChunk(**p.model_dump()) for p in parents])
        return len(parents)

    async def _write_vector_children(
        self, doc_id: str, version: int, children: Sequence[ChildChunkPayload]
    ) -> int:
        """委托 vector_store.upsert_children。"""
        return await self.vector_store.upsert_children(doc_id, version, children)

    async def _write_keyword_children(
        self, doc_id: str, version: int, children: Sequence[ChildChunkPayload]
    ) -> int:
        """委托 keyword_store.upsert_children。"""
        return await self.keyword_store.upsert_children(doc_id, version, children)

    async def _rollback_shadow_version(self, doc_id: str, version: int) -> None:
        """并发删除三端 version == v+1 的数据，并将台账 status 置 FAILED（current_version 不动）。

        使用 return_exceptions=True；任一端删除失败抛 RollbackError（残留由下次 GC 兜底）。
        """
        # Never delete data that another writer has already published.
        async with self.session_factory() as session:
            current = await self._load_document(session, doc_id)
            if current and current.current_version == version:
                return
        results = await asyncio.gather(self._delete_parents(doc_id, version, stale=False), self.vector_store.delete_version(doc_id, version), self.keyword_store.delete_version(doc_id, version), return_exceptions=True)
        try:
            await self._mark_failed(doc_id, version-1)
        except Exception as exc:
            results.append(exc)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise RollbackError(str(errors[0]), doc_id=doc_id, version=version) from errors[0]

    # ------------------------------------------------------------------
    # ⑥ 原子切换
    # ------------------------------------------------------------------

    async def _switch_current_version(
        self, doc_id: str, expected_version: int, new_version: int, content_hash: str
    ) -> None:
        """乐观锁 CAS 切换 current_version。

        UPDATE kb_documents SET current_version=:new, content_hash=:hash, status='active', updated_at=now()
        WHERE doc_id=:doc_id AND current_version=:expected
        rowcount != 1 -> 抛 VersionConflictError。
        """
        async with self.session_factory.begin() as session:
            result = await session.execute(update(KBDocument).where(KBDocument.doc_id == doc_id, KBDocument.current_version == expected_version).values(current_version=new_version, content_hash=content_hash, status=DocumentStatus.ACTIVE))
            if result.rowcount != 1:
                raise VersionConflictError("CAS failed", doc_id=doc_id, version=new_version)

    # ------------------------------------------------------------------
    # ⑦ 旧版本异步 GC
    # ------------------------------------------------------------------

    def _schedule_gc(self, doc_id: str, keep_version: int) -> asyncio.Task[None] | None:
        """gc_enabled 时 asyncio.create_task(_garbage_collect)，加入 _gc_tasks 并注册 done 回调移除。"""
        if not self.gc_enabled:
            return None
        task = asyncio.create_task(self._garbage_collect(doc_id, keep_version))
        self._gc_tasks.add(task)
        task.add_done_callback(self._gc_tasks.discard)
        return task

    async def _garbage_collect(self, doc_id: str, keep_version: int) -> None:
        """并发清理三端 version != keep_version 的数据：
        PG DELETE kb_parent_chunks WHERE doc_id=? AND version<>?；
        vector_store / keyword_store.delete_stale_versions。

        GC 前再次校验 current_version == keep_version（防止被更新版本超车后误删），不一致则放弃。
        异常仅记录日志（GarbageCollectionError），不向上传播。
        """
        try:
            # Hold the document lock throughout deletion, so a later publish cannot
            # overtake the check while an adapter is awaiting external I/O.
            async with self._doc_locks.setdefault(doc_id, asyncio.Lock()):
                async with self.session_factory() as session:
                    document = await self._load_document(session, doc_id)
                    if not document or document.current_version != keep_version:
                        return
                results = await asyncio.gather(self._delete_parents(doc_id, keep_version, stale=True), self.vector_store.delete_stale_versions(doc_id, keep_version), self.keyword_store.delete_stale_versions(doc_id, keep_version), return_exceptions=True)
                errors = [r for r in results if isinstance(r, BaseException)]
                if errors:
                    raise GarbageCollectionError(str(errors[0]), doc_id=doc_id, version=keep_version)
        except Exception as exc:
            logging.getLogger(__name__).warning("%s", GarbageCollectionError(str(exc), doc_id=doc_id, version=keep_version).to_hint())


    async def _mark_failed(self, doc_id: str, expected_version: int) -> None:
        async with self.session_factory.begin() as session:
            await session.execute(update(KBDocument).where(KBDocument.doc_id == doc_id, KBDocument.current_version == expected_version, KBDocument.status == DocumentStatus.SYNCING).values(status=DocumentStatus.FAILED))

    async def _delete_parents(self, doc_id: str, version: int, *, stale: bool) -> None:
        async with self.session_factory.begin() as session:
            condition = KBParentChunk.version != version if stale else KBParentChunk.version == version
            await session.execute(delete(KBParentChunk).where(KBParentChunk.doc_id == doc_id, condition))
