"""异步双路混合检索服务。

链路（以单次 search 为例）：

    search(request)
      │
      ├─ ① 前置过滤构造：读 kb_documents，筛选
      │      status ∈ SERVABLE_STATUSES AND current_version >= 1
      │      AND dept_id == user.dept_id AND allowed_roles ∩ user.roles ≠ ∅
      │      -> RetrievalFilter(doc_versions = {doc_id: current_version})
      │      可见文档为空 -> 直接返回空结果（不调用任何索引）
      │
      ├─ ② 查询向量化：EmbeddingPort.embed_texts([query])（失败 -> 向量分支降级）
      │
      ├─ ③ 双路并发召回（asyncio.gather + 单路 wait_for 超时）：
      │      VectorSearchPort.search ‖ KeywordSearchPort.search
      │      单路超时/异常 -> 该路结果置空，degraded=True；两路皆失败 -> RetrievalUnavailableError
      │
      ├─ ④ 纵深防御校验：丢弃 (doc_id, version) 不在 doc_versions 中的候选（适配器 bug 兜底）
      │
      ├─ ⑤ RRF 融合 + parent_id 聚合去重 -> list[FusedCandidate]
      │
      ├─ ⑥ 批量回表 kb_parent_chunks：WHERE (parent_id, version) 精确匹配
      │      缺失父块（版本切换后已被 GC）直接丢弃并标记 PARENT_MISSING
      │
      └─ ⑦ RerankerPort 精排（超时/异常 -> 按 RRF 顺序截断，degraded=True）-> 截断 top_k

不变量：
- 输出的每个父块 version 必须等于 ① 时刻读到的 current_version，永不返回影子版本或已下线文档。
- 输出的每个父块所属文档必须满足 ① 的 ACL 条件。
- 降级只影响召回完整度，不放松任何过滤条件。
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextvars import ContextVar
from time import perf_counter
from collections.abc import Sequence
from sqlalchemy import select
from data_platform.storage_sync.models import KBDocument, KBParentChunk
from data_platform.retrieval_engine.errors import (
    RetrievalError, LedgerUnavailableError, FilterTooBroadError, RetrievalUnavailableError,
)
from data_platform.retrieval_engine.models import SERVABLE_STATUSES, RetrievalBranch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_platform.retrieval_engine.models import (
    DegradedReason,
    FusedCandidate,
    RetrievalCandidate,
    RetrievalConfig,
    RetrievalFilter,
    RetrievedParentChunk,
    SearchRequest,
    SearchResponse,
    UserContext,
)
from data_platform.retrieval_engine.ports import KeywordSearchPort, RerankerPort, VectorSearchPort
from data_platform.storage_sync.ports import EmbeddingPort


logger = logging.getLogger(__name__)
_stages: ContextVar[dict[str, float] | None] = ContextVar("retrieval_stages", default=None)


class _EmbeddingFailed(Exception):
    pass


async def _timed(name, awaitable):
    start = perf_counter()
    try:
        return await awaitable
    finally:
        stages = _stages.get()
        if stages is not None:
            stages[name] = (perf_counter() - start) * 1000



_REASON_TEXT = {
    DegradedReason.EMBEDDING_FAILED: "查询向量化失败，仅使用关键词召回",
    DegradedReason.VECTOR_TIMEOUT: "向量召回超时",
    DegradedReason.VECTOR_ERROR: "向量召回失败",
    DegradedReason.KEYWORD_TIMEOUT: "关键词召回超时",
    DegradedReason.KEYWORD_ERROR: "关键词召回失败",
    DegradedReason.RERANK_TIMEOUT: "精排超时，使用融合排序",
    DegradedReason.RERANK_ERROR: "精排失败，使用融合排序",
    DegradedReason.PARENT_MISSING: "部分父块已不可用，已丢弃",
}


class HybridRetrievalService:
    """ACL 前置过滤 + 向量/BM25 双路召回 + RRF 融合 + 父块回表 + 交叉编码器精排。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingPort,
        vector_search: VectorSearchPort,
        keyword_search: KeywordSearchPort,
        reranker: RerankerPort,
        config: RetrievalConfig | None = None,
    ) -> None:
        """注入依赖。config 为 None 时使用 RetrievalConfig() 默认值。

        服务本身无可变状态，可在多个并发请求间共享同一实例。
        """
        self.session_factory = session_factory
        self.embedder = embedder
        self.vector_search = vector_search
        self.keyword_search = keyword_search
        self.reranker = reranker
        self.config = config or RetrievalConfig()

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def search(self, request: SearchRequest) -> SearchResponse:
        """检索主入口（编排 ①~⑦，见模块文档）。

        - 使用 time.perf_counter 统计端到端与分阶段耗时，写入 latency_ms / stage_latency_ms。
        - 降级原因去重后按发生顺序写入 degraded_reasons，degraded_reason 为其中文说明以 "; " 拼接。
        - 仅以下异常向上抛出（由 FastAPI 层转为 ToolErrorResponse）：
            LedgerUnavailableError / FilterTooBroadError / RetrievalUnavailableError。
        - 其他任何下游异常不得逃逸：召回/精排失败走降级路径。
        """
        start = perf_counter()
        stages = {}
        token = _stages.set(stages)
        try:
            retrieval_filter = await _timed("filter", self._build_filter(request.user_context))
            counts = {"vector": 0, "keyword": 0, "fused": 0}
            reasons = []
            results = []
            if retrieval_filter.doc_versions:
                vec, kw, reasons, recall_stages = await self._recall(request.query, retrieval_filter)
                stages.update(recall_stages)
                fusion_start = perf_counter()
                vec = self._enforce_filter(vec, retrieval_filter)
                kw = self._enforce_filter(kw, retrieval_filter)
                fused = self._fuse_rrf(vec, kw, self.config.rrf_k)
                stages["fusion"] = (perf_counter() - fusion_start) * 1000
                counts = {"vector": len(vec), "keyword": len(kw), "fused": len(fused)}
                parents, missing = await _timed("fetch", self._fetch_parents(fused))
                if missing:
                    reasons.append(DegradedReason.PARENT_MISSING)
                if request.enable_rerank and parents:
                    results, reason = await _timed("rerank", self._rerank(request.query, parents, request.top_k))
                    if reason:
                        reasons.append(reason)
                else:
                    results = parents[:request.top_k]
            reasons = list(dict.fromkeys(reasons))
            return SearchResponse(query=request.query, results=results, degraded=bool(reasons),
                degraded_reasons=reasons, degraded_reason="; ".join(_REASON_TEXT[r] for r in reasons) or None,
                latency_ms=(perf_counter() - start) * 1000, stage_latency_ms=stages, candidate_counts=counts)
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalUnavailableError("检索处理失败，请稍后重试") from exc
        finally:
            _stages.reset(token)

    # ------------------------------------------------------------------
    # ① 前置 ACL 与有效版本过滤
    # ------------------------------------------------------------------

    async def _build_filter(self, user: UserContext) -> RetrievalFilter:
        """从 kb_documents 构造 RetrievalFilter。

        SQL 条件：status IN SERVABLE_STATUSES AND current_version >= 1 AND dept_id == user.dept_id；
        allowed_roles 为 JSON 列，角色交集判断在 Python 侧完成（allowed_roles 为空即不可见）。
        只 SELECT doc_id / current_version / allowed_roles 三列，不加载 ORM 实体。
        台账读取异常 -> LedgerUnavailableError（fail-closed）；可见文档数 > max_filter_docs -> FilterTooBroadError。
        """
        try:
            async with self.session_factory() as session:
                rows = (await session.execute(select(KBDocument.doc_id, KBDocument.current_version,
                    KBDocument.allowed_roles).where(KBDocument.status.in_(SERVABLE_STATUSES),
                    KBDocument.current_version >= 1, KBDocument.dept_id == user.dept_id))).all()
            versions = {row.doc_id: row.current_version for row in rows if set(user.roles).intersection(row.allowed_roles)}
        except Exception as exc:
            raise LedgerUnavailableError("无法读取权限台账") from exc
        if len(versions) > self.config.max_filter_docs:
            raise FilterTooBroadError("可见文档数超过过滤上限")
        return RetrievalFilter(dept_id=user.dept_id, roles=user.roles, doc_versions=versions)

    # ------------------------------------------------------------------
    # ②③ 向量化与双路并发召回
    # ------------------------------------------------------------------

    async def _recall(
        self, query: str, retrieval_filter: RetrievalFilter
    ) -> tuple[list[RetrievalCandidate], list[RetrievalCandidate], list[DegradedReason], dict[str, float]]:
        """并发执行两路召回，返回 (vector_candidates, keyword_candidates, degraded_reasons, stage_latency)。

        - 向量分支 = 向量化 + ANN，作为一个协程整体参与 gather，从而与 BM25 真正并行。
        - asyncio.gather(..., return_exceptions=True)；每路内部 asyncio.wait_for 独立超时。
        - TimeoutError -> *_TIMEOUT，其他 Exception -> *_ERROR（logging 记录堆栈）；CancelledError 必须原样上抛。
        - 两路都失败 -> RetrievalUnavailableError；任一路成功（即使 0 条结果）不视为失败。
        """
        stages = {}
        token = _stages.set(stages)
        try:
            outcomes = await asyncio.gather(self._run_vector_branch(query, retrieval_filter),
                self._run_keyword_branch(query, retrieval_filter), return_exceptions=True)
        finally:
            _stages.reset(token)
        reasons = []
        results = []
        for index, outcome in enumerate(outcomes):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, Exception):
                if isinstance(outcome, _EmbeddingFailed):
                    reason = DegradedReason.EMBEDDING_FAILED
                elif isinstance(outcome, TimeoutError):
                    reason = (DegradedReason.VECTOR_TIMEOUT, DegradedReason.KEYWORD_TIMEOUT)[index]
                else:
                    reason = (DegradedReason.VECTOR_ERROR, DegradedReason.KEYWORD_ERROR)[index]
                reasons.append(reason)
                logger.warning("召回分支降级: %s", reason.value, exc_info=(type(outcome), outcome, outcome.__traceback__))
                results.append([])
            else:
                results.append(outcome)
        if len(reasons) == 2:
            raise RetrievalUnavailableError("两路召回均不可用")
        return results[0], results[1], reasons, stages

    async def _run_vector_branch(
        self, query: str, retrieval_filter: RetrievalFilter
    ) -> list[RetrievalCandidate]:
        """向量化（embedding_timeout_s）后调用 VectorSearchPort.search（branch_timeout_s）。

        向量化失败需与 ANN 失败区分：抛出时让 _recall 能映射为 EMBEDDING_FAILED。
        """
        try:
            vectors = await _timed("embedding", asyncio.wait_for(
                self.embedder.embed_texts([query]), self.config.embedding_timeout_s))
            if len(vectors) != 1 or len(vectors[0]) != self.embedder.dimension or not all(math.isfinite(x) for x in vectors[0]):
                raise ValueError("invalid query embedding")
        except Exception as exc:
            raise _EmbeddingFailed() from exc
        return await _timed("vector", asyncio.wait_for(self.vector_search.search(
            vectors[0], retrieval_filter, self.config.vector_top_k), self.config.branch_timeout_s))

    async def _run_keyword_branch(
        self, query: str, retrieval_filter: RetrievalFilter
    ) -> list[RetrievalCandidate]:
        """调用 KeywordSearchPort.search（branch_timeout_s）。"""
        return await _timed("keyword", asyncio.wait_for(self.keyword_search.search(
            query, retrieval_filter, self.config.keyword_top_k), self.config.branch_timeout_s))

    # ------------------------------------------------------------------
    # ④ 纵深防御
    # ------------------------------------------------------------------

    @staticmethod
    def _enforce_filter(
        candidates: Sequence[RetrievalCandidate], retrieval_filter: RetrievalFilter
    ) -> list[RetrievalCandidate]:
        """丢弃 doc_versions.get(doc_id) != version 的候选，保持原顺序；有丢弃时记 warning 日志。

        这是适配器 pre-filter 之外的第二道防线，不能替代 pre-filter。纯函数。
        """
        kept = [c for c in candidates if retrieval_filter.doc_versions.get(c.doc_id) == c.version]
        if len(kept) != len(candidates):
            logger.warning("过滤丢弃 %d 条无效候选", len(candidates) - len(kept))
        return kept

    # ------------------------------------------------------------------
    # ⑤ RRF 融合
    # ------------------------------------------------------------------

    @staticmethod
    def _fuse_rrf(
        vector_candidates: Sequence[RetrievalCandidate],
        keyword_candidates: Sequence[RetrievalCandidate],
        rrf_k: int,
    ) -> list[FusedCandidate]:
        """倒数秩融合并按 parent_id 聚合去重。纯函数、确定性。

        算法：
        1. 每路内部先按 parent_id 去重：父块在该路的名次 = 其子块首次出现的顺序（1 起，连续重排），
           即同一父块多个子块命中只计一次、取最好名次。
        2. rrf_score(parent) = Σ_branch 1 / (rrf_k + rank_branch(parent))，未出现的分支不计分。
        3. 按 rrf_score 降序；同分按 parent_id 升序打破平局。
        4. source_branches 按 RetrievalBranch 定义顺序（VECTOR, KEYWORD）输出。
        """
        fused = {}
        for branch, candidates in ((RetrievalBranch.VECTOR, vector_candidates), (RetrievalBranch.KEYWORD, keyword_candidates)):
            unique = {}
            for candidate in candidates:
                unique.setdefault(candidate.parent_id, candidate)
            for rank, candidate in enumerate(unique.values(), 1):
                previous = fused.get(candidate.parent_id)
                fused[candidate.parent_id] = FusedCandidate(parent_id=candidate.parent_id,
                    doc_id=candidate.doc_id, version=candidate.version,
                    rrf_score=(previous.rrf_score if previous else 0) + 1 / (rrf_k + rank),
                    source_branches=(previous.source_branches if previous else []) + [branch])
        return sorted(fused.values(), key=lambda c: (-c.rrf_score, c.parent_id))

    # ------------------------------------------------------------------
    # ⑥ 父块回表
    # ------------------------------------------------------------------

    async def _fetch_parents(
        self, fused: Sequence[FusedCandidate]
    ) -> tuple[list[RetrievedParentChunk], bool]:
        """批量回表 kb_parent_chunks（单条 SELECT ... WHERE parent_id IN (...)），返回 (parents, any_missing)。

        - 结果按 fused 的顺序输出；score 暂填 rrf_score，rerank_score=None。
        - 行的 version 必须等于 FusedCandidate.version，否则视为缺失（防跨版本串读）。
        - fused 为空时不访问数据库。
        """
        selected = list(fused[:self.config.rerank_candidates])
        if not selected:
            return [], False
        try:
            async with self.session_factory() as session:
                rows = (await session.scalars(select(KBParentChunk).where(
                    KBParentChunk.parent_id.in_([c.parent_id for c in selected])))).all()
            by_id = {row.parent_id: row for row in rows}
            results = []
            for candidate in selected:
                row = by_id.get(candidate.parent_id)
                if row is None or row.version != candidate.version or row.doc_id != candidate.doc_id:
                    continue
                results.append(RetrievedParentChunk(parent_id=row.parent_id, doc_id=row.doc_id,
                    version=row.version, section_path=row.section_path, full_content=row.full_content,
                    page_number=row.page_number, score=candidate.rrf_score, rrf_score=candidate.rrf_score,
                    source_branches=candidate.source_branches))
            return results, len(results) != len(selected)
        except Exception as exc:
            raise RetrievalUnavailableError("父块读取暂时不可用") from exc

    # ------------------------------------------------------------------
    # ⑦ 精排
    # ------------------------------------------------------------------

    async def _rerank(
        self, query: str, parents: Sequence[RetrievedParentChunk], top_k: int
    ) -> tuple[list[RetrievedParentChunk], DegradedReason | None]:
        """交叉编码器精排并截断 top_k。

        - passages = [f"{section_path}\\n{full_content}"]（section_path 为空时不加前缀）。
        - 成功：写入 rerank_score，score = rerank_score，按 rerank_score 降序（同分保留 RRF 顺序）。
        - rerank_timeout_s 超时或异常、或返回长度与输入不一致 -> 保持 RRF 顺序截断，
          返回 RERANK_TIMEOUT / RERANK_ERROR。
        """
        try:
            passages = [f"{p.section_path}\n{p.full_content}" if p.section_path else p.full_content for p in parents]
            scores = await asyncio.wait_for(self.reranker.rerank(query, passages), self.config.rerank_timeout_s)
            if len(scores) != len(parents) or not all(math.isfinite(s) for s in scores):
                raise ValueError("invalid reranker scores")
            results = [p.model_copy(update={"score": s, "rerank_score": s}) for p, s in zip(parents, scores)]
            return sorted(results, key=lambda p: -p.score)[:top_k], None
        except Exception as exc:
            logger.warning("精排降级", exc_info=True)
            reason = DegradedReason.RERANK_TIMEOUT if isinstance(exc, TimeoutError) else DegradedReason.RERANK_ERROR
            return list(parents[:top_k]), reason
