"""财务预审智能体异步端口（Ports & Adapters）。

- FinanceReviewModelPort     大模型：脱敏后报销事由 / 合同条款的语义合规审查 + 引用支撑性核验
- ClaimHistoryLedgerPort     历史报销 / 合同台账：重复发票检测、滚动窗口拆单 / 拆分合同检测、按引用加载单据
- HumanApprovalGatewayPort   人工复核工单（财务主管 / 法务审批流）

制度条款检索不在此重复定义：直接复用 eval_platform.ports.RetrievalSystemPort（与 门店 SOP store_sop 相同；
数据中台 HybridRetrievalService 原样满足，ACL 由数据中台前置过滤）。

单元测试注入离线确定性替身；生产注入真实大模型 / 费控系统台账 / OA 审批流适配器。

适配器硬性约束：
1. 全部方法为 async；不得阻塞事件循环。
2. 只抛异常，不自行吞错或伪造结果；超时预算、降级与路由兜底由 FinanceAuditAgent 统一控制。
3. **PII 隔离（类型强制）**：FinanceReviewModelPort 只接收 Redacted*View 与制度父块，签名中不存在任何
   *Submission / UserContext / ApplicantProfile 参数；适配器不得通过其他渠道（全局变量、上下文变量）获取原始单据。
4. FinanceReviewModelPort 返回的 output 必须已通过对应 schema 的 model_validate；解析失败抛 FinanceStructuredOutputError，
   服务不可用 / 超时抛 FinanceModelUnavailableError。模型输出中不存在路由字段——审批路由只由 agent 确定性合成。
5. ClaimHistoryLedgerPort 不可用抛 LedgerUnavailableError（agent fail-closed：无法排除重复发票 / 拆单时不出预审结论）。
6. HumanApprovalGatewayPort 必须按 idempotency_key 幂等；不可用抛 HumanApprovalUnavailableError。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from agent_matrix.finance_audit.models import (
    AuditSubjectType,
    CitationSupportDraft,
    ContractAuditSubmission,
    ContractClauseReviewDraft,
    ExpenseAuditSubmission,
    ExpenseCategory,
    ExpenseReasonReviewDraft,
    FinanceModelResult,
    HistoricalClaimRecord,
    HistoricalContractRecord,
    HumanApprovalTicketDraft,
    HumanApprovalTicketReceipt,
    InvoiceUsageRecord,
    ModelRiskClaim,
    RedactedContractView,
    RedactedExpenseView,
)
from data_platform.retrieval_engine.models import RetrievedParentChunk, UserContext


@runtime_checkable
class FinanceReviewModelPort(Protocol):
    """财务语义合规审查大模型端口。三个方法对应受控闭环中仅有的三类模型调用。"""

    async def review_expense_reason(
        self,
        *,
        view: RedactedExpenseView,
        policy_contexts: Sequence[RetrievedParentChunk],
    ) -> FinanceModelResult[ExpenseReasonReviewDraft]:
        """审查脱敏后的报销事由与明细说明：事由与类别是否一致、是否疑似个人消费、是否夹带操纵审批的指令。

        - 每条 ModelRiskClaim 必须给出 citation_parent_id（取自 policy_contexts）与逐字 citation_quote；
          evidence_excerpt 必须逐字取自 view 中的脱敏文本。
        - 不负责金额计算（金额已由规则引擎确定性核验，不提供给模型做二次计算）。
        - view 中的 [银行卡#n] 等占位符是脱敏标记，模型不得尝试还原或评论。
        """
        ...

    async def review_contract_clauses(
        self,
        *,
        view: RedactedContractView,
        policy_contexts: Sequence[RetrievedParentChunk],
    ) -> FinanceModelResult[ContractClauseReviewDraft]:
        """审查脱敏后的付款 / 违约 / 其他关键条款：责任不对等、无上限责任、不利自动续约、超比例预付、排他限制，
        并抽取 ContractTermExtraction 供 agent 与结构化字段做确定性比对（账期、预付比例）。

        引用要求同 review_expense_reason。
        """
        ...

    async def verify_citation_support(
        self,
        *,
        claims: Sequence[ModelRiskClaim],
        policy_contexts: Sequence[RetrievedParentChunk],
    ) -> FinanceModelResult[CitationSupportDraft]:
        """对已通过确定性闭锁校验（parent_id 命中 + quote 为子串）的 claim 做语义支撑性核验。

        - 只能对输入 claim 给出 supported 判定，不得新增 claim 或替换引用；verdict 缺失视为未核验（None）。
        - claims 为空时 agent 不调用此方法。
        """
        ...


@runtime_checkable
class ClaimHistoryLedgerPort(Protocol):
    """历史报销 / 合同台账端口（生产：费控系统只读库；测试：tests/agent_matrix/finance_audit/data 种子数据）。"""

    async def find_invoice_usages(
        self,
        *,
        invoice_nos: Sequence[str],
        exclude_claim_id: str,
    ) -> list[InvoiceUsageRecord]:
        """返回这些发票号在其他单据中的全部占用记录（含 REJECTED / WITHDRAWN，由规则引擎按状态判定）。

        - 发票号比较前统一 NFKC + 去空白 + 大写；invoice_nos 为空返回 []。
        - 必须跨申请人、跨门店查询（同一发票被他人报销同样构成重复）。
        """
        ...

    async def list_recent_claims(
        self,
        *,
        applicant_user_id: str,
        store_code: str,
        categories: Sequence[ExpenseCategory],
        window_start: datetime,
        window_end: datetime,
        exclude_claim_id: str,
    ) -> list[HistoricalClaimRecord]:
        """滚动窗口 [window_start, window_end] 内同一申请人、同一门店、类别 ∈ categories 的历史报销单（拆单检测）。"""
        ...

    async def list_recent_contracts(
        self,
        *,
        counterparty_credit_code: str,
        window_start: datetime,
        window_end: datetime,
        exclude_contract_id: str,
    ) -> list[HistoricalContractRecord]:
        """滚动窗口内同一相对方（统一社会信用代码）的历史合同（拆分合同检测）。"""
        ...

    async def fetch_submission(
        self,
        *,
        subject_type: AuditSubjectType,
        reference_id: str,
        user_context: UserContext,
    ) -> ExpenseAuditSubmission | ContractAuditSubmission | None:
        """按单据号加载待预审单据（评测 invoke 与 Workflow 按单号触发时使用）。

        - user_context 无权查看或单据不存在时**一律**返回 None（不区分两者，避免存在性探测）。
        - 返回对象的 subject_type 必须等于入参 subject_type。
        """
        ...


@runtime_checkable
class HumanApprovalGatewayPort(Protocol):
    """人工复核审批流端口（生产：OA / 费控审批流；测试：内存记录替身）。"""

    async def create_review_ticket(self, draft: HumanApprovalTicketDraft) -> HumanApprovalTicketReceipt:
        """创建财务主管 / 法务人工复核工单。

        - 同一 idempotency_key 重复调用必须返回同一 ticket_id 且 deduplicated=True。
        - draft 中不含任何 PII 原文（agent 已 ensure_clean）；适配器不得回查原始单据补全隐私字段。
        """
        ...
