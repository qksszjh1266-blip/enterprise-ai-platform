"""供应链 BI 智能体异步端口（Ports & Adapters）。

- SqlGeneratorModelPort      大模型：意图识别 / 基于语义字典生成 SQL / 报错自纠错改写 / 分析结论归纳
- ReadOnlySqlSandboxPort     只读 SQL 执行沙箱：强制超时、最大返回行数、底层只读保护
- WarehouseScopeResolverPort 可信行级权限解析：UserContext → WarehouseScopeFilter（组织权限系统）

单元测试注入离线确定性替身；生产注入真实大模型 / PostgreSQL 只读从库 / 组织权限服务适配器。

适配器硬性约束：
1. 全部方法为 async；不得阻塞事件循环（同步驱动必须换用 aiosqlite / asyncpg 等异步客户端）。
2. 只抛异常，不自行吞错或伪造结果；超时预算、重试与降级由 SupplyChainBIAgent 统一控制。
3. SqlGeneratorModelPort 返回的 output 必须已通过对应 schema 的 model_validate；解析失败抛 StructuredOutputError；
   服务不可用 / 超时抛 SqlModelUnavailableError。模型端口不得接收 WarehouseScopeFilter 的取值或 UserContext。
4. ReadOnlySqlSandboxPort 只接收已通过 SqlStaticGuard 的 normalized_sql；SQL 层错误以 SqlExecutionResult
   (status != SUCCESS, error_hint) 返回，基础设施故障抛 SandboxUnavailableError。
5. 只读保护必须在**连接层**实现，不得依赖守卫：
   - SQLite：`file:...?mode=ro` URI 打开 + `PRAGMA query_only = ON`；progress handler 到期中断；
   - PostgreSQL：专用只读角色 + 只读从库；每次执行 `BEGIN READ ONLY` + `SET LOCAL statement_timeout`，执行后 ROLLBACK。
6. 执行结果单元格只允许 ScalarValue；date / datetime 转 ISO 字符串，Decimal 转 float。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from data_platform.retrieval_engine.models import UserContext
from agent_matrix.supply_chain_bi.models import (
    AnalysisIntent,
    AnalysisNarrativeDraft,
    InventoryAlert,
    ResultTable,
    ScalarValue,
    SemanticContext,
    SqlAttempt,
    SqlCandidate,
    SqlDialect,
    SqlExecutionResult,
    SqlModelResult,
    WarehouseScopeFilter,
)


@runtime_checkable
class SqlGeneratorModelPort(Protocol):
    """Text-to-SQL 大模型端口。四个方法对应受控循环中仅有的四处模型调用。"""

    async def classify_intent(
        self,
        *,
        question: str,
        catalog: SemanticContext,
    ) -> SqlModelResult[AnalysisIntent]:
        """意图识别 + 标准指标匹配。

        - metric_ids 只能取自 catalog.metrics；目录外的 ID 由 agent 丢弃，不由适配器纠正。
        - 识别到采购底价 / 合同价 / 毛利底表等诉求时返回 SENSITIVE_REQUEST。
        """
        ...

    async def generate_sql(
        self,
        *,
        question: str,
        intent: AnalysisIntent,
        context: SemanticContext,
    ) -> SqlModelResult[SqlCandidate]:
        """基于语义字典生成单条 SELECT / WITH 候选 SQL。

        - 必须按 context.scope_instructions 使用 :scope_region_id / :scope_warehouse_ids / :as_of_date 绑定参数。
        - 只允许引用 context.tables 中的表与列。
        """
        ...

    async def repair_sql(
        self,
        *,
        question: str,
        intent: AnalysisIntent,
        context: SemanticContext,
        previous_attempts: Sequence[SqlAttempt],
    ) -> SqlModelResult[SqlCandidate]:
        """依据上一次失败的 repair_feedback（守卫 error_hint 或沙箱 error_hint）改写 SQL。

        - previous_attempts 已按时间排序，最后一条为最近失败；FATAL 违规不会进入此方法。
        """
        ...

    async def summarize_analysis(
        self,
        *,
        question: str,
        intent: AnalysisIntent,
        context: SemanticContext,
        result: ResultTable,
        alerts: Sequence[InventoryAlert],
        scope_summary: str,
    ) -> SqlModelResult[AnalysisNarrativeDraft]:
        """归纳自然语言分析结论；每个数值必须以 KeyFigure 锚定 result 的 (row_index, column)。"""
        ...


@runtime_checkable
class ReadOnlySqlSandboxPort(Protocol):
    """只读 SQL 执行沙箱端口。"""

    @property
    def dialect(self) -> SqlDialect:
        """沙箱方言，须与 SemanticCatalogSpec.dialect 一致（agent 装配时校验）。"""
        ...

    async def execute(
        self,
        *,
        sql: str,
        bind_params: Mapping[str, ScalarValue | Sequence[str]],
        timeout_s: float,
        max_rows: int,
    ) -> SqlExecutionResult:
        """在只读连接上执行一条已守卫的 SQL。

        - 最多 fetch max_rows + 1 行：超出则丢弃多余行并 truncated=True。
        - 超时：引擎侧中断 + asyncio.timeout 双保险，返回 status=TIMEOUT（不抛异常）。
        - Sequence 类型参数按 expanding IN 绑定；未在 sql 中出现的参数忽略。
        - 引擎拒绝写操作 → status=READ_ONLY_VIOLATION。
        - error_hint 只描述可修正信息（如 "no such column: inv.qty，可用列: on_hand_qty, ..."），不含连接串 / 堆栈。
        """
        ...

    async def latest_snapshot_date(self) -> date:
        """返回库存快照表的最新 snapshot_date（as_of_date 缺省时使用）；失败抛 SandboxUnavailableError。"""
        ...


@runtime_checkable
class WarehouseScopeResolverPort(Protocol):
    """可信行级权限解析端口（生产：组织架构 / 数据权限服务；测试：静态映射表）。"""

    async def resolve(self, user_context: UserContext) -> WarehouseScopeFilter:
        """按 user_id + roles 解析授权范围。

        - 无任何授权抛 ScopeDeniedError；权限系统不可用抛 ScopeResolutionError（fail-closed）。
        - 返回值的 user_id 必须等于 user_context.user_id。
        """
        ...
