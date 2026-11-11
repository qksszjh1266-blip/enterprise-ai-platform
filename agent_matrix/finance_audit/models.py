"""财务费用报销与商品采购合同合规预审智能体数据模型与传输契约。

本模块仅包含 Pydantic V2 模型、枚举、类型别名与策略常量，不含任何 I/O：

金额类型（Decimal 精度铁律）
- Money / NonNegativeMoney / PositiveMoney / TaxRate / Quantity
  一律 decimal.Decimal；输入为 float 直接 ValidationError（JSON 中金额必须以字符串传入，如 "128.50"）；
  JSON 序列化输出为字符串，全链路不经过二进制浮点。

枚举与策略常量
- AuditSubjectType / ExpenseCategory / InvoiceType / AttachmentType / ContractType
- RiskLevel / ApprovalRoute / ApproverTier / ReviewerQueue / FindingSource / ViolationCode
- InvoiceUsageStatus / PiiType / ModelReviewStatus / AuditStage
- ViolationPolicy / VIOLATION_POLICIES      每个违规编码的最低风险等级、最低审批路由、是否红线、允许来源
- REDLINE_VIOLATION_CODES / REJECT_VIOLATION_CODES / MANUAL_REVIEW_RISK_LEVELS / ACTIVE_INVOICE_USAGE_STATUSES
- DEFAULT_APPROVAL_BANDS / DEFAULT_CATEGORY_RULES / DEFAULT_ALLOWED_TAX_RATES

输入
- ApplicantProfile / InvoiceInfo / ExpenseLineItem / ExpenseAuditSubmission
- CounterpartyInfo / PaymentInstallment / ContractAuditSubmission
- AuditSubmission（按 subject_type 判别的联合类型）
- FinanceAuditPayload（HTTP 体）/ FinanceAuditRequest（agent 内部入口）

隐私脱敏（redaction.py 使用）
- PiiMatch / RedactionResult / RedactedExpenseLine / RedactedExpenseView / RedactedContractView

历史台账（ports.ClaimHistoryLedgerPort 使用）
- InvoiceUsageRecord / HistoricalClaimRecord / HistoricalContractRecord / LedgerSnapshot

确定性规则（rule_engine.py 使用）
- ApprovalBand / CategoryRule / FinanceRuleConfig
- LineAmountCheck / InvoiceTotalCheck / DuplicateInvoiceFlag / SplitClaimEvidence / PaymentScheduleCheck
- AmountReconciliationResult / ComplianceFinding / RuleEvaluation

模型传输（ports.FinanceReviewModelPort 使用；全部 extra="forbid"，不含身份字段、不含审批路由字段）
- FinanceModelResult[T] / ModelRiskClaim / ExpenseReasonReviewDraft / ContractTermExtraction
- ContractClauseReviewDraft / CitationSupportVerdict / CitationSupportDraft

人工审批（ports.HumanApprovalGatewayPort 使用）
- HumanApprovalTicketDraft / HumanApprovalTicketReceipt

编排中间结果与输出
- PolicyRetrievalResult / CitationCheckResult / ModelReviewOutcome / RouteDecision
- AuditTrailSummary / FinanceAuditResponse / FinanceAuditOutcome / FinanceAuditConfig

关键约定（实现与测试必须遵守）：
1. **算术不一致是 finding，不是 ValidationError**：明细合计、税额、发票总额、付款计划合计等勾稽偏差由
   DeterministicFinanceRuleEngine 产出 ComplianceFinding（进入审批路由），模型层只校验结构完整性与引用完整性，
   否则带错单据会被 422 挡在门外、无法给出"需补正"结论。
2. **审批路由只由确定性代码合成**：模型输出 schema 不含 route / risk 放行字段；模型 claim 的风险等级只能被
   VIOLATION_POLICIES 抬高、不能降低；REJECT 只能由 RULE_ENGINE 来源的 REJECT_VIOLATION_CODES 触发
   （模型最多把单据升级为 ESCALATE_HUMAN）。
3. **红线一票否决**：任一 is_redline finding 或 HIGH / CRITICAL finding ⇒ 路由 ∈ {ESCALATE_HUMAN, REJECT}；
   FinanceAuditResponse 校验器保证这一点无法被构造绕过。
4. **申请人 ≠ 调用人**：ApplicantProfile 是单据业务事实（用于自审冲突、运营主管本人申请升级审批人等规则），
   绝不参与 ACL；ACL 身份只来自 UserContext（网关头 / AgentInvocation）。
5. **原始 PII 只存在于 *Submission 输入模型**：Redacted*View、模型端口参数、AuditTrailSummary、
   FinanceAuditResponse、HumanApprovalTicketDraft、日志中不得出现银行卡号 / 身份证号 / 手机号原文。

复用（只读，不得复制或改写）：
- data_platform.retrieval_engine.models.UserContext / RetrievedParentChunk / DegradedReason
- agent_matrix.store_sop.models.SopCitation（制度条款引用）
- eval_platform.models.TokenUsage

兼容性要求：公共类名与模型字段保持稳定，变更需同步调用方与契约测试。
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from agent_matrix.store_sop.models import SopCitation
from data_platform.retrieval_engine.models import DegradedReason, RetrievedParentChunk, UserContext
from eval_platform.models import TokenUsage

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# 金额类型（Decimal 精度铁律）
# ---------------------------------------------------------------------------


def _reject_binary_float(value: object) -> object:
    """拒绝 float 输入（二进制浮点无法精确表示 0.1 等十进制金额）；str / int / Decimal 放行。

    这是类型层的输入守卫，不是业务规则；JSON 数字字面量（如 128.5）会被解析为 float 而被拒绝，
    调用方必须以字符串传入金额与税率。
    """
    if isinstance(value, float):
        raise ValueError('金额 / 税率 / 数量禁止使用浮点数，请以字符串传入，如 "128.50"、"0.13"')
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(_reject_binary_float),
    Field(max_digits=14, decimal_places=2, allow_inf_nan=False),
]
"""人民币金额：最多 12 位整数 + 2 位小数（分）。"""

NonNegativeMoney = Annotated[Money, Field(ge=0)]
PositiveMoney = Annotated[Money, Field(gt=0)]

TaxRate = Annotated[
    Decimal,
    BeforeValidator(_reject_binary_float),
    Field(ge=0, le=Decimal("0.17"), max_digits=5, decimal_places=4, allow_inf_nan=False),
]
"""增值税税率（小数形式，0.13 表示 13%）；是否在白名单内由规则引擎判定（finding，而非 ValidationError）。"""

Quantity = Annotated[
    Decimal,
    BeforeValidator(_reject_binary_float),
    Field(gt=0, max_digits=12, decimal_places=4, allow_inf_nan=False),
]

MONEY_QUANTUM = Decimal("0.01")
"""金额量化单位（分）；所有中间计算结果用 quantize(MONEY_QUANTUM, ROUND_HALF_UP) 归一，见 rule_engine.to_money。"""

_REF_PATTERN = r"^[A-Za-z0-9_.:-]+$"

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class AuditSubjectType(str, enum.Enum):
    """预审对象类型（与 AuditSubmission 判别字段 subject_type 取值一致）。"""

    EXPENSE = "expense"  # 部门费用报销单
    CONTRACT = "contract"  # 商品采购 / 仓储物流类合同


class ExpenseCategory(str, enum.Enum):
    """费用类别（对应《财务费用报销制度》费用审批矩阵与差旅标准）。"""

    DAILY_CONSUMABLES = "daily_consumables"  # 日常耗材
    EQUIPMENT_REPAIR = "equipment_repair"  # 设备维修
    RENOVATION = "renovation"  # 装修改造
    TRAVEL_LODGING = "travel_lodging"  # 跨城差旅住宿（每晚限额）
    LOCAL_TRANSPORT = "local_transport"  # 市内交通（据实报销）
    OTHER = "other"


class InvoiceType(str, enum.Enum):
    VAT_SPECIAL = "vat_special"  # 增值税专用发票
    VAT_NORMAL = "vat_normal"  # 增值税普通发票
    E_INVOICE_FULL = "e_invoice_full"  # 全面数字化电子发票（20 位号码）
    OTHER = "other"  # 定额发票、行程单等


class AttachmentType(str, enum.Enum):
    """单据附件类型（制度要求的附件清单由 CategoryRule.required_attachments 声明）。"""

    INVOICE = "invoice"  # 发票
    PURCHASE_LIST = "purchase_list"  # 采购清单
    ACCEPTANCE_RECORD = "acceptance_record"  # 验收记录 / 验收单
    MAINTENANCE_TICKET = "maintenance_ticket"  # 维保工单
    QUOTATION = "quotation"  # 报价单
    BUDGET_APPROVAL = "budget_approval"  # 预算审批
    CONTRACT = "contract"  # 合同
    PRE_APPROVAL = "pre_approval"  # 事前审批（超标住宿书面批准）
    BUSINESS_VOUCHER = "business_voucher"  # 其他业务凭证


class ContractType(str, enum.Enum):
    FRANCHISE_PROCUREMENT = "franchise_procurement"  # 外部供应商商品 / 设备采购
    EQUIPMENT_PURCHASE = "equipment_purchase"  # 集团设备采购
    STORE_LEASE = "store_lease"  # 仓库租赁
    RENOVATION_SERVICE = "renovation_service"  # 装修工程服务
    LOGISTICS_SERVICE = "logistics_service"  # 物流配送服务
    OTHER = "other"


class RiskLevel(str, enum.Enum):
    """合规风险等级（HIGH / CRITICAL 严禁自动放行）。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        """严重度序：LOW=0 < MEDIUM=1 < HIGH=2 < CRITICAL=3。"""
        return _RISK_RANK[self]


_RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class ApprovalRoute(str, enum.Enum):
    """预审后的审批路由。

    AUTO_APPROVE 表示"预审自动通过、按 approver_tier 进入常规审批流"，**不等于**直接付款。
    """

    AUTO_APPROVE = "AUTO_APPROVE"
    NEEDS_SUPPLEMENT = "NEEDS_SUPPLEMENT"  # 退回申请人补正附件 / 金额 / 说明
    ESCALATE_HUMAN = "ESCALATE_HUMAN"  # 转财务主管 / 法务人工复核（必须生成人工审批工单）
    REJECT = "REJECT"  # 确定性规则证实违规（如重复发票已支付），直接退回

    @property
    def rank(self) -> int:
        """严格度序：AUTO_APPROVE=0 < NEEDS_SUPPLEMENT=1 < ESCALATE_HUMAN=2 < REJECT=3；最终路由取各 finding 的 max。"""
        return _ROUTE_RANK[self]


_ROUTE_RANK: dict[ApprovalRoute, int] = {
    ApprovalRoute.AUTO_APPROVE: 0,
    ApprovalRoute.NEEDS_SUPPLEMENT: 1,
    ApprovalRoute.ESCALATE_HUMAN: 2,
    ApprovalRoute.REJECT: 3,
}


class ApproverTier(str, enum.Enum):
    """常规审批层级（来自《财务费用报销制度 > 费用审批矩阵》与合同金额门槛）。"""

    STORE_MANAGER = "store_manager"  # 运营主管
    REGIONAL_MANAGER = "regional_manager"  # 区域经理（运营主管本人申请时由运营主管升级至此）
    REGIONAL_MANAGER_AND_FINANCE = "regional_manager_and_finance"  # 区域经理及财务
    REGIONAL_HEAD_AND_FINANCE_HEAD = "regional_head_and_finance_head"  # 区域负责人及财务负责人
    LEGAL_AND_FINANCE_HEAD = "legal_and_finance_head"  # 合同：法务 + 财务负责人

    @property
    def rank(self) -> int:
        return _TIER_RANK[self]


_TIER_RANK: dict[ApproverTier, int] = {
    ApproverTier.STORE_MANAGER: 0,
    ApproverTier.REGIONAL_MANAGER: 1,
    ApproverTier.REGIONAL_MANAGER_AND_FINANCE: 2,
    ApproverTier.REGIONAL_HEAD_AND_FINANCE_HEAD: 3,
    ApproverTier.LEGAL_AND_FINANCE_HEAD: 4,
}


class ReviewerQueue(str, enum.Enum):
    """人工复核队列。"""

    FINANCE_SUPERVISOR = "finance_supervisor"  # 财务主管（报销类）
    LEGAL_COUNSEL = "legal_counsel"  # 法务（合同条款类）
    FINANCE_AND_LEGAL = "finance_and_legal"  # 金额与条款风险并存


class FindingSource(str, enum.Enum):
    RULE_ENGINE = "rule_engine"  # DeterministicFinanceRuleEngine（Decimal 确定性）
    MODEL_REVIEW = "model_review"  # FinanceReviewModelPort 语义审查（必须经闭锁引用校验）
    SYSTEM_GUARD = "system_guard"  # 编排层兜底（模型 / 制度检索不可用、引用无法核验）


class ViolationCode(str, enum.Enum):
    """稳定违规编码（机器可读；评测 expected_keywords 以 "code:{value}" 引用）。"""

    # ---- 报销：金额勾稽（RULE_ENGINE）----
    LINE_AMOUNT_MISMATCH = "FIN_LINE_AMOUNT_MISMATCH"  # 数量 × 单价 ≠ 不含税金额 / 含税 ≠ 不含税 + 税额
    TAX_RATE_NOT_ALLOWED = "FIN_TAX_RATE_NOT_ALLOWED"  # 税率不在白名单
    TAX_AMOUNT_MISMATCH = "FIN_TAX_AMOUNT_MISMATCH"  # 税额 ≠ 不含税金额 × 税率（超容差）
    INVOICE_TOTAL_MISMATCH = "FIN_INVOICE_TOTAL_MISMATCH"  # 明细按发票汇总 ≠ 发票票面金额
    DECLARED_TOTAL_MISMATCH = "FIN_DECLARED_TOTAL_MISMATCH"  # 申报总额 ≠ 明细合计
    # ---- 报销：重复 / 拆单（RULE_ENGINE，红线）----
    DUPLICATE_INVOICE_PAID = "FIN_DUPLICATE_INVOICE_PAID"  # 发票已在其他单据审批通过 / 已支付
    DUPLICATE_INVOICE_IN_FLIGHT = "FIN_DUPLICATE_INVOICE_IN_FLIGHT"  # 发票正被其他在途单据占用
    DUPLICATE_INVOICE_IN_SUBMISSION = "FIN_DUPLICATE_INVOICE_IN_SUBMISSION"  # 同一单据内发票号重复
    SUSPECTED_SPLIT_CLAIM = "FIN_SUSPECTED_SPLIT_CLAIM"  # 滚动窗口内拆单规避审批额度
    # ---- 报销：制度合规（RULE_ENGINE）----
    CATEGORY_LIMIT_EXCEEDED = "FIN_CATEGORY_LIMIT_EXCEEDED"  # 单笔超出该费用类别额度（如日常耗材 > 500）
    MISSING_ATTACHMENT = "FIN_MISSING_ATTACHMENT"  # 缺少制度要求附件
    SUBMISSION_OVERDUE = "FIN_SUBMISSION_OVERDUE"  # 超过费用发生后 30 天提交时限
    LODGING_OVER_CAP = "FIN_LODGING_OVER_CAP"  # 住宿超标且无事前审批
    SELF_REVIEW_CONFLICT = "FIN_SELF_REVIEW_CONFLICT"  # 调用人即申请人（不得审批本人报销）
    # ---- 合同：确定性（RULE_ENGINE）----
    PAYMENT_SCHEDULE_MISMATCH = "CTR_PAYMENT_SCHEDULE_MISMATCH"  # 付款计划合计 ≠ 标的额
    PAYMENT_TERM_EXCEEDED = "CTR_PAYMENT_TERM_EXCEEDED"  # 账期超出上限
    BREACH_CLAUSE_MISSING = "CTR_BREACH_CLAUSE_MISSING"  # 违约条款为空
    SUSPECTED_SPLIT_CONTRACT = "CTR_SUSPECTED_SPLIT_CONTRACT"  # 同一相对方窗口期内拆分合同规避法务审查
    LEGAL_REVIEW_REQUIRED = "CTR_LEGAL_REVIEW_REQUIRED"  # 标的额超过法务审查门槛（路由需要，非违规）
    # ---- 双来源（RULE_ENGINE 关键词扫描 / MODEL_REVIEW 语义识别）----
    PROMPT_INJECTION_SUSPECTED = "SEC_PROMPT_INJECTION_SUSPECTED"  # 事由 / 条款中夹带操纵审批的指令
    # ---- 语义审查（MODEL_REVIEW）----
    REASON_BUSINESS_INCONSISTENT = "FIN_REASON_BUSINESS_INCONSISTENT"  # 事由与费用类别 / 明细不符
    PERSONAL_CONSUMPTION_SUSPECTED = "FIN_PERSONAL_CONSUMPTION_SUSPECTED"  # 疑似个人消费
    CLAUSE_FIELD_MISMATCH = "CTR_CLAUSE_FIELD_MISMATCH"  # 条款文本与结构化字段不一致（如账期）
    UNBALANCED_PENALTY_CLAUSE = "CTR_UNBALANCED_PENALTY_CLAUSE"  # 违约责任显著不对等
    UNLIMITED_LIABILITY = "CTR_UNLIMITED_LIABILITY"  # 我方承担无上限责任
    UNFAVORABLE_AUTO_RENEWAL = "CTR_UNFAVORABLE_AUTO_RENEWAL"  # 自动续约且无退出窗口
    EXCESSIVE_PREPAYMENT = "CTR_EXCESSIVE_PREPAYMENT"  # 预付款比例过高
    EXCLUSIVITY_RESTRICTION = "CTR_EXCLUSIVITY_RESTRICTION"  # 排他 / 独家供货限制
    # ---- 编排兜底（SYSTEM_GUARD）----
    MODEL_REVIEW_UNAVAILABLE = "SYS_MODEL_REVIEW_UNAVAILABLE"  # 语义审查模型不可用 / 输出非法
    POLICY_CONTEXT_UNAVAILABLE = "SYS_POLICY_CONTEXT_UNAVAILABLE"  # 制度检索不可用或无可引用条款
    CITATION_UNVERIFIED = "SYS_CITATION_UNVERIFIED"  # 高风险模型 claim 的制度引用未通过闭锁校验


class InvoiceUsageStatus(str, enum.Enum):
    """历史台账中发票占用状态。"""

    SUBMITTED = "submitted"  # 在途
    APPROVED = "approved"
    PAID = "paid"
    REJECTED = "rejected"  # 已驳回（释放发票占用）
    WITHDRAWN = "withdrawn"  # 已撤回（释放发票占用）


class PiiType(str, enum.Enum):
    BANK_CARD = "bank_card"  # 16~19 位且通过 Luhn 校验
    ID_CARD = "id_card"  # 18 位居民身份证（GB 11643 校验位）
    MOBILE_PHONE = "mobile_phone"  # 中国大陆 11 位手机号（可带 +86 / 86 前缀）


class ModelReviewStatus(str, enum.Enum):
    COMPLETED = "completed"  # 模型审查完成且引用核验已执行
    SKIPPED_BY_REJECT = "skipped_by_reject"  # 确定性规则已判 REJECT，一票否决，跳过模型（节省 token）
    NO_POLICY_CONTEXT = "no_policy_context"  # 无可引用制度条款，未调用模型（路由兜底 ESCALATE_HUMAN）
    MODEL_UNAVAILABLE = "model_unavailable"  # 模型超时 / 不可用 / 结构化输出非法（路由兜底 ESCALATE_HUMAN）


class AuditStage(str, enum.Enum):
    """stage_latency_ms 的键。"""

    REDACT = "redact"
    LEDGER_LOOKUP = "ledger_lookup"
    RULE_CHECK = "rule_check"
    RETRIEVE_POLICY = "retrieve_policy"
    MODEL_REVIEW = "model_review"
    VERIFY_CITATIONS = "verify_citations"
    ROUTE = "route"
    ESCALATE = "escalate"


# ---------------------------------------------------------------------------
# 策略常量
# ---------------------------------------------------------------------------


class ViolationPolicy(BaseModel):
    """单个违规编码的处置下限（不可由模型或配置放宽）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_risk_level: RiskLevel
    min_route: ApprovalRoute
    is_redline: bool = False
    allowed_sources: frozenset[FindingSource]

    @model_validator(mode="after")
    def _check_policy(self) -> ViolationPolicy:
        if self.is_redline and self.min_risk_level is not RiskLevel.CRITICAL:
            raise ValueError("红线编码的最低风险等级必须为 CRITICAL")
        if self.min_risk_level.rank >= RiskLevel.HIGH.rank and self.min_route.rank < ApprovalRoute.ESCALATE_HUMAN.rank:
            raise ValueError("HIGH / CRITICAL 编码的最低路由必须 >= ESCALATE_HUMAN")
        if self.min_route is ApprovalRoute.REJECT and self.allowed_sources != frozenset({FindingSource.RULE_ENGINE}):
            raise ValueError("REJECT 级编码只能由 RULE_ENGINE 产生")
        return self


_R = frozenset({FindingSource.RULE_ENGINE})
_M = frozenset({FindingSource.MODEL_REVIEW})
_RM = frozenset({FindingSource.RULE_ENGINE, FindingSource.MODEL_REVIEW})
_S = frozenset({FindingSource.SYSTEM_GUARD})


def _vp(risk: RiskLevel, route: ApprovalRoute, sources: frozenset[FindingSource], redline: bool = False) -> ViolationPolicy:
    return ViolationPolicy(min_risk_level=risk, min_route=route, is_redline=redline, allowed_sources=sources)


_L, _MD, _H, _C = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL
_SUP, _ESC, _REJ = ApprovalRoute.NEEDS_SUPPLEMENT, ApprovalRoute.ESCALATE_HUMAN, ApprovalRoute.REJECT

VIOLATION_POLICIES: dict[ViolationCode, ViolationPolicy] = {
    ViolationCode.LINE_AMOUNT_MISMATCH: _vp(_MD, _SUP, _R),
    ViolationCode.TAX_RATE_NOT_ALLOWED: _vp(_H, _ESC, _R),
    ViolationCode.TAX_AMOUNT_MISMATCH: _vp(_MD, _SUP, _R),
    ViolationCode.INVOICE_TOTAL_MISMATCH: _vp(_MD, _SUP, _R),
    ViolationCode.DECLARED_TOTAL_MISMATCH: _vp(_MD, _SUP, _R),
    ViolationCode.DUPLICATE_INVOICE_PAID: _vp(_C, _REJ, _R, redline=True),
    ViolationCode.DUPLICATE_INVOICE_IN_FLIGHT: _vp(_C, _ESC, _R, redline=True),
    ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION: _vp(_MD, _SUP, _R),
    ViolationCode.SUSPECTED_SPLIT_CLAIM: _vp(_C, _ESC, _R, redline=True),
    ViolationCode.CATEGORY_LIMIT_EXCEEDED: _vp(_MD, _ESC, _R),
    ViolationCode.MISSING_ATTACHMENT: _vp(_L, _SUP, _R),
    ViolationCode.SUBMISSION_OVERDUE: _vp(_MD, _ESC, _R),
    ViolationCode.LODGING_OVER_CAP: _vp(_MD, _SUP, _R),
    ViolationCode.SELF_REVIEW_CONFLICT: _vp(_H, _ESC, _R),
    ViolationCode.PAYMENT_SCHEDULE_MISMATCH: _vp(_H, _ESC, _R),
    ViolationCode.PAYMENT_TERM_EXCEEDED: _vp(_MD, _ESC, _R),
    ViolationCode.BREACH_CLAUSE_MISSING: _vp(_MD, _SUP, _R),
    ViolationCode.SUSPECTED_SPLIT_CONTRACT: _vp(_C, _ESC, _R, redline=True),
    ViolationCode.LEGAL_REVIEW_REQUIRED: _vp(_L, _ESC, _R),
    ViolationCode.PROMPT_INJECTION_SUSPECTED: _vp(_C, _ESC, _RM, redline=True),
    ViolationCode.REASON_BUSINESS_INCONSISTENT: _vp(_MD, _SUP, _M),
    ViolationCode.PERSONAL_CONSUMPTION_SUSPECTED: _vp(_H, _ESC, _M),
    ViolationCode.CLAUSE_FIELD_MISMATCH: _vp(_MD, _SUP, _M),
    ViolationCode.UNBALANCED_PENALTY_CLAUSE: _vp(_MD, _ESC, _M),
    ViolationCode.UNLIMITED_LIABILITY: _vp(_H, _ESC, _M),
    ViolationCode.UNFAVORABLE_AUTO_RENEWAL: _vp(_MD, _ESC, _M),
    ViolationCode.EXCESSIVE_PREPAYMENT: _vp(_H, _ESC, _M),
    ViolationCode.EXCLUSIVITY_RESTRICTION: _vp(_MD, _ESC, _M),
    ViolationCode.MODEL_REVIEW_UNAVAILABLE: _vp(_MD, _ESC, _S),
    ViolationCode.POLICY_CONTEXT_UNAVAILABLE: _vp(_MD, _ESC, _S),
    ViolationCode.CITATION_UNVERIFIED: _vp(_H, _ESC, _S),
}
"""违规编码 → 处置下限。ComplianceFinding 的 risk_level / min_route 只能 >= 此表，is_redline 必须相等。"""

REDLINE_VIOLATION_CODES: frozenset[ViolationCode] = frozenset(c for c, p in VIOLATION_POLICIES.items() if p.is_redline)
"""红线编码（一票否决自动放行）：重复发票（已付 / 在途）、拆单、拆分合同、提示词注入。"""

REJECT_VIOLATION_CODES: frozenset[ViolationCode] = frozenset(
    c for c, p in VIOLATION_POLICIES.items() if p.min_route is ApprovalRoute.REJECT
)
"""唯一可导致 REJECT 的编码集合（当前仅 DUPLICATE_INVOICE_PAID，对应制度"重复发票退回申请人，不得重复支付"）。"""

MANUAL_REVIEW_RISK_LEVELS: frozenset[RiskLevel] = frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})
"""出现这些等级的 finding 时，严禁 AUTO_APPROVE / NEEDS_SUPPLEMENT。"""

ACTIVE_INVOICE_USAGE_STATUSES: frozenset[InvoiceUsageStatus] = frozenset(
    {InvoiceUsageStatus.SUBMITTED, InvoiceUsageStatus.APPROVED, InvoiceUsageStatus.PAID}
)
"""占用发票的台账状态；REJECTED / WITHDRAWN 已释放，不构成重复。"""

DEFAULT_ALLOWED_TAX_RATES: frozenset[Decimal] = frozenset(
    Decimal(r) for r in ("0", "0.01", "0.03", "0.06", "0.09", "0.13")
)
"""增值税税率白名单（含小规模纳税人 1% / 3% 征收率）。"""

# ---------------------------------------------------------------------------
# 输入：报销
# ---------------------------------------------------------------------------


class ApplicantProfile(BaseModel):
    """申请人业务身份（单据事实，**不参与 ACL**）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    user_id: str = Field(..., min_length=1, max_length=128)
    dept_id: str = Field(..., min_length=1, max_length=64)
    roles: list[str] = Field(..., min_length=1, description="如 store_clerk / store_manager / regional_manager")


class InvoiceInfo(BaseModel):
    """发票票面信息（结构化字段，不经过 PII 脱敏；发票号与纳税人识别号不属于个人隐私）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    invoice_no: str = Field(..., pattern=r"^[0-9A-Za-z]{8,20}$", description="发票号码（全电发票 20 位）")
    invoice_code: str | None = Field(default=None, pattern=r"^[0-9]{10,12}$", description="发票代码（全电发票无）")
    invoice_type: InvoiceType
    issue_date: date
    seller_name: str = Field(..., min_length=1, max_length=200)
    seller_tax_id: str = Field(..., pattern=r"^[0-9A-Z]{15,20}$", description="销售方纳税人识别号")
    amount_excl_tax: NonNegativeMoney
    tax_amount: NonNegativeMoney
    amount_incl_tax: PositiveMoney


class ExpenseLineItem(BaseModel):
    """报销明细行。金额间的算术一致性由规则引擎核验（finding），此处不校验。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    line_no: int = Field(..., ge=1, le=200)
    category: ExpenseCategory
    description: str = Field(..., min_length=1, max_length=200, description="明细说明（可能含 PII，送模型前脱敏）")
    invoice_no: str = Field(..., pattern=r"^[0-9A-Za-z]{8,20}$", description="关联发票号，必须出现在 invoices 中")
    occurred_on: date = Field(..., description="费用发生日（30 天提交时限的起算日）")
    quantity: Quantity = Field(default=Decimal("1"))
    unit: str = Field(default="项", max_length=16, description="如 件 / 晚 / 次")
    unit_price: NonNegativeMoney
    amount_excl_tax: NonNegativeMoney
    tax_rate: TaxRate
    tax_amount: NonNegativeMoney
    amount_incl_tax: PositiveMoney


class ExpenseAuditSubmission(BaseModel):
    """运营费用报销预审输入。

    原始 PII 可能出现在 reason / lines[*].description（自由文本）与 payee_bank_account（结构化）；
    后者永不进入 RedactedExpenseView（仅保留 has_payee_bank_account）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    subject_type: Literal["expense"] = "expense"
    claim_id: str = Field(..., min_length=1, max_length=128, pattern=_REF_PATTERN)
    store_code: str = Field(..., min_length=1, max_length=32)
    applicant: ApplicantProfile
    currency: Literal["CNY"] = "CNY"
    reason: str = Field(..., min_length=1, max_length=2000, description="报销事由（原文，可能含 PII / 注入文本）")
    lines: list[ExpenseLineItem] = Field(..., min_length=1, max_length=200)
    invoices: list[InvoiceInfo] = Field(..., min_length=1, max_length=100)
    declared_total: PositiveMoney = Field(..., description="申请人申报的含税总额")
    attachments: list[AttachmentType] = Field(default_factory=list)
    pre_approval_ref: str | None = Field(default=None, max_length=128, description="事前审批单号（超标住宿）")
    payee_bank_account: str | None = Field(default=None, max_length=32, description="收款银行卡号（原始 PII）")
    submitted_at: AwareDatetime = Field(..., description="提交时间；规则引擎以此为基准（时限、滚动窗口），不读系统时钟")

    @model_validator(mode="after")
    def _check_structure(self) -> ExpenseAuditSubmission:
        if [line.line_no for line in self.lines] != list(range(1, len(self.lines) + 1)):
            raise ValueError("lines.line_no 必须从 1 连续递增")
        invoice_nos = {inv.invoice_no for inv in self.invoices}
        dangling = sorted({line.invoice_no for line in self.lines} - invoice_nos)
        if dangling:
            raise ValueError(f"明细引用了未登记的发票号: {', '.join(dangling)}")
        return self


# ---------------------------------------------------------------------------
# 输入：合同
# ---------------------------------------------------------------------------


class CounterpartyInfo(BaseModel):
    """合同相对方。contact_phone / bank_account 为原始 PII，永不进入 RedactedContractView。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=200)
    unified_credit_code: str = Field(..., pattern=r"^[0-9A-Z]{18}$", description="统一社会信用代码")
    is_franchisee: bool = Field(default=False, description="相对方是否为本集团战略合作商")
    contact_phone: str | None = Field(default=None, max_length=32)
    bank_account: str | None = Field(default=None, max_length=32)


class PaymentInstallment(BaseModel):
    """付款计划分期。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    seq: int = Field(..., ge=1, le=120)
    due_in_days: int = Field(..., ge=0, le=3650, description="相对合同生效日的付款天数（0 = 预付款）")
    amount: PositiveMoney
    description: str = Field(default="", max_length=200)


class ContractAuditSubmission(BaseModel):
    """商品采购 / 仓储物流合同预审输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    subject_type: Literal["contract"] = "contract"
    contract_id: str = Field(..., min_length=1, max_length=128, pattern=_REF_PATTERN)
    contract_type: ContractType
    title: str = Field(..., min_length=1, max_length=200)
    store_code: str | None = Field(default=None, max_length=32)
    applicant: ApplicantProfile
    counterparty: CounterpartyInfo
    currency: Literal["CNY"] = "CNY"
    subject_amount: PositiveMoney = Field(..., description="合同标的额（含税）")
    tax_rate: TaxRate | None = None
    payment_term_days: int = Field(..., ge=0, le=3650, description="结构化账期（天）")
    payment_schedule: list[PaymentInstallment] = Field(default_factory=list, max_length=120)
    payment_terms_text: str = Field(..., min_length=1, max_length=4000, description="付款 / 账期条款原文")
    breach_clause_text: str = Field(default="", max_length=8000, description="违约条款原文；为空由规则引擎产出 finding")
    other_key_clauses: str = Field(default="", max_length=8000, description="续约 / 排他 / 责任限制等其他关键条款原文")
    effective_date: date
    expiry_date: date
    attachments: list[AttachmentType] = Field(default_factory=list)
    submitted_at: AwareDatetime

    @model_validator(mode="after")
    def _check_structure(self) -> ContractAuditSubmission:
        if self.expiry_date < self.effective_date:
            raise ValueError("expiry_date 不得早于 effective_date")
        if [p.seq for p in self.payment_schedule] != list(range(1, len(self.payment_schedule) + 1)):
            raise ValueError("payment_schedule.seq 必须从 1 连续递增")
        return self


AuditSubmission = Annotated[ExpenseAuditSubmission | ContractAuditSubmission, Field(discriminator="subject_type")]
"""按 subject_type（"expense" / "contract"）判别的预审对象联合类型。"""

# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class FinanceAuditRequest(BaseModel):
    """预审请求（agent 内部入口）。user_context 必须来自网关头或 AgentInvocation。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    submission: AuditSubmission
    user_context: UserContext = Field(..., description="调用人（财务 / 法务）身份：制度检索 ACL 与自审冲突判定依据")
    trace_id: str | None = Field(default=None, max_length=256)


class FinanceAuditPayload(BaseModel):
    """HTTP 请求体；user_context 仅在 allow_body_identity=True 的内网调试下生效，否则以网关头为准。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    submission: AuditSubmission
    user_context: UserContext | None = None
    trace_id: str | None = Field(default=None, max_length=256)


# ---------------------------------------------------------------------------
# 隐私脱敏
# ---------------------------------------------------------------------------


class PiiMatch(BaseModel):
    """单处 PII 命中（只记录位置与占位符，**不记录原文**）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pii_type: PiiType
    start: int = Field(..., ge=0, description="原文中的起始下标（含）")
    end: int = Field(..., ge=1, description="原文中的结束下标（不含）")
    placeholder: str = Field(..., description="替换后的占位符，如 [银行卡#1]")


class RedactionResult(BaseModel):
    """单段文本脱敏结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(..., description="脱敏后文本")
    matches: list[PiiMatch] = Field(default_factory=list, description="按 start 升序、互不重叠")
    counts: dict[PiiType, int] = Field(default_factory=dict)


class RedactedExpenseLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line_no: int = Field(..., ge=1)
    category: ExpenseCategory
    description: str = Field(..., description="脱敏后明细说明")
    quantity: Quantity
    unit: str
    amount_incl_tax: PositiveMoney
    tax_rate: TaxRate


class RedactedExpenseView(BaseModel):
    """送入 FinanceReviewModelPort 的报销单脱敏视图：不含申请人 user_id、收款账号与任何 PII 原文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    store_code: str
    applicant_roles: list[str]
    categories: list[ExpenseCategory] = Field(..., min_length=1, description="去重后按首次出现排序")
    reason: str = Field(..., description="脱敏后报销事由")
    lines: list[RedactedExpenseLine] = Field(..., min_length=1)
    declared_total: PositiveMoney
    invoice_count: int = Field(..., ge=1)
    attachments: list[AttachmentType] = Field(default_factory=list)
    has_pre_approval: bool = False
    has_payee_bank_account: bool = False
    pii_counts: dict[PiiType, int] = Field(default_factory=dict, description="整单 PII 命中计数（含结构化字段）")


class RedactedContractView(BaseModel):
    """送入 FinanceReviewModelPort 的合同脱敏视图：不含相对方联系电话 / 银行账号与任何 PII 原文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    contract_type: ContractType
    title: str
    counterparty_name: str = Field(..., description="相对方名称（个体工商户名称可能含人名，同样经脱敏器处理）")
    counterparty_is_franchisee: bool
    subject_amount: PositiveMoney
    tax_rate: TaxRate | None = None
    payment_term_days: int = Field(..., ge=0)
    payment_schedule: list[PaymentInstallment] = Field(default_factory=list)
    payment_terms_text: str
    breach_clause_text: str
    other_key_clauses: str
    effective_date: date
    expiry_date: date
    pii_counts: dict[PiiType, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 历史台账
# ---------------------------------------------------------------------------


class InvoiceUsageRecord(BaseModel):
    """发票在历史台账中的一次占用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice_no: str
    claim_id: str
    applicant_user_id: str
    status: InvoiceUsageStatus
    submitted_at: AwareDatetime


class HistoricalClaimRecord(BaseModel):
    """滚动窗口内的历史报销单（拆单检测输入）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    applicant_user_id: str
    store_code: str
    category: ExpenseCategory = Field(..., description="单据主类别（金额最大的明细类别）")
    amount_incl_tax: PositiveMoney
    invoice_nos: list[str] = Field(default_factory=list)
    status: InvoiceUsageStatus
    submitted_at: AwareDatetime


class HistoricalContractRecord(BaseModel):
    """滚动窗口内同一相对方的历史合同（拆分合同检测输入）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    counterparty_credit_code: str
    contract_type: ContractType
    subject_amount: PositiveMoney
    status: InvoiceUsageStatus
    submitted_at: AwareDatetime


class LedgerSnapshot(BaseModel):
    """一次预审所用的台账快照（agent 用 asyncio.gather 并发拉取后组装；规则引擎只读此快照，无 I/O）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_type: AuditSubjectType
    window_start: AwareDatetime
    window_end: AwareDatetime = Field(..., description="== submission.submitted_at")
    invoice_usages: list[InvoiceUsageRecord] = Field(default_factory=list, description="已排除本单 claim_id")
    recent_claims: list[HistoricalClaimRecord] = Field(default_factory=list, description="已排除本单 claim_id")
    recent_contracts: list[HistoricalContractRecord] = Field(default_factory=list, description="已排除本合同")

    @model_validator(mode="after")
    def _check_window(self) -> LedgerSnapshot:
        if self.window_start > self.window_end:
            raise ValueError("window_start 不得晚于 window_end")
        if self.subject_type is AuditSubjectType.EXPENSE and self.recent_contracts:
            raise ValueError("报销快照不得包含合同记录")
        if self.subject_type is AuditSubjectType.CONTRACT and (self.invoice_usages or self.recent_claims):
            raise ValueError("合同快照不得包含报销 / 发票记录")
        return self


# ---------------------------------------------------------------------------
# 规则配置
# ---------------------------------------------------------------------------


class ApprovalBand(BaseModel):
    """审批金额区间（左开右闭）：lower_exclusive < amount <= upper_inclusive；upper 为 None 表示无上限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lower_exclusive: NonNegativeMoney
    upper_inclusive: PositiveMoney | None = None
    approver_tier: ApproverTier

    @model_validator(mode="after")
    def _check_band(self) -> ApprovalBand:
        if self.upper_inclusive is not None and self.upper_inclusive <= self.lower_exclusive:
            raise ValueError("upper_inclusive 必须大于 lower_exclusive")
        return self


class CategoryRule(BaseModel):
    """费用类别规则：单笔额度上限与必备附件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: ExpenseCategory
    max_single_amount: PositiveMoney | None = Field(default=None, description="超出即 CATEGORY_LIMIT_EXCEEDED；None 不限")
    required_attachments: list[AttachmentType] = Field(default_factory=list)


DEFAULT_APPROVAL_BANDS: tuple[ApprovalBand, ...] = (
    ApprovalBand(lower_exclusive=Decimal("0"), upper_inclusive=Decimal("500"), approver_tier=ApproverTier.STORE_MANAGER),
    ApprovalBand(
        lower_exclusive=Decimal("500"),
        upper_inclusive=Decimal("3000"),
        approver_tier=ApproverTier.REGIONAL_MANAGER_AND_FINANCE,
    ),
    ApprovalBand(lower_exclusive=Decimal("3000"), approver_tier=ApproverTier.REGIONAL_HEAD_AND_FINANCE_HEAD),
)
"""《费用审批矩阵》：≤500 运营主管；500~3000 区域经理及财务；>3000 区域负责人及财务负责人。

制度原文"501 至 3000 元"按左开右闭区间 (500, 3000] 解释，消除 500.01~500.99 的空档。
"""

DEFAULT_CATEGORY_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        category=ExpenseCategory.DAILY_CONSUMABLES,
        max_single_amount=Decimal("500"),
        required_attachments=[AttachmentType.INVOICE, AttachmentType.PURCHASE_LIST, AttachmentType.ACCEPTANCE_RECORD],
    ),
    CategoryRule(
        category=ExpenseCategory.EQUIPMENT_REPAIR,
        max_single_amount=Decimal("3000"),
        required_attachments=[AttachmentType.MAINTENANCE_TICKET, AttachmentType.QUOTATION, AttachmentType.INVOICE],
    ),
    CategoryRule(
        category=ExpenseCategory.RENOVATION,
        required_attachments=[
            AttachmentType.BUDGET_APPROVAL,
            AttachmentType.CONTRACT,
            AttachmentType.ACCEPTANCE_RECORD,
            AttachmentType.INVOICE,
        ],
    ),
    CategoryRule(category=ExpenseCategory.TRAVEL_LODGING, required_attachments=[AttachmentType.INVOICE]),
    CategoryRule(category=ExpenseCategory.LOCAL_TRANSPORT, required_attachments=[AttachmentType.INVOICE]),
    CategoryRule(category=ExpenseCategory.OTHER, required_attachments=[AttachmentType.INVOICE]),
)


class FinanceRuleConfig(BaseModel):
    """DeterministicFinanceRuleEngine 参数。所有金额阈值为 Decimal；红线与路由下限不在此配置（见 VIOLATION_POLICIES）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruleset_version: str = Field(default="finance_rules.v1", min_length=1, max_length=64)
    approval_bands: tuple[ApprovalBand, ...] = DEFAULT_APPROVAL_BANDS
    category_rules: tuple[CategoryRule, ...] = DEFAULT_CATEGORY_RULES
    allowed_tax_rates: frozenset[Decimal] = DEFAULT_ALLOWED_TAX_RATES
    line_amount_tolerance: NonNegativeMoney = Field(default=Decimal("0.01"), description="数量×单价 与 不含税金额 的容差")
    line_tax_tolerance: NonNegativeMoney = Field(default=Decimal("0.01"), description="单行税额容差（逐行四舍五入）")
    invoice_tax_tail_tolerance: NonNegativeMoney = Field(
        default=Decimal("0.06"), description="同一发票明细税额合计与票面税额的尾差容差"
    )
    declared_total_tolerance: NonNegativeMoney = Field(default=Decimal("0.00"), description="申报总额必须分毫不差")
    submission_deadline_days: int = Field(default=30, ge=1, le=366, description="费用发生后提交时限（天）")
    lodging_cap_per_night: PositiveMoney = Field(default=Decimal("350"), description="跨城住宿每晚上限")
    split_window_days: int = Field(default=7, ge=1, le=90, description="报销拆单滚动窗口（天）")
    contract_split_window_days: int = Field(default=90, ge=1, le=365, description="合同拆分滚动窗口（天）")
    contract_legal_review_threshold: PositiveMoney = Field(
        default=Decimal("50000"), description="标的额 > 此值须法务审查（LEGAL_REVIEW_REQUIRED）"
    )
    max_payment_term_days: int = Field(default=90, ge=0, le=3650, description="账期上限（天）")
    prompt_injection_markers: tuple[str, ...] = Field(
        default=(
            "忽略以上",
            "忽略之前",
            "忽略所有规则",
            "直接通过",
            "自动通过",
            "无需审核",
            "跳过审核",
            "ignore previous",
            "ignore all",
            "auto approve",
            "system prompt",
        ),
        description="确定性注入关键词（NFKC + casefold 后子串匹配）；模型侧语义识别作为补充",
    )

    @model_validator(mode="after")
    def _check_bands(self) -> FinanceRuleConfig:
        bands = self.approval_bands
        if not bands or bands[0].lower_exclusive != Decimal("0") or bands[-1].upper_inclusive is not None:
            raise ValueError("approval_bands 必须从 0 起始且最后一档无上限")
        for prev, nxt in zip(bands, bands[1:]):
            if prev.upper_inclusive != nxt.lower_exclusive:
                raise ValueError("approval_bands 必须首尾相接、无空档无重叠")
        if len({r.category for r in self.category_rules}) != len(self.category_rules):
            raise ValueError("category_rules 中费用类别不得重复")
        return self


# ---------------------------------------------------------------------------
# 规则结果（金额勾稽）
# ---------------------------------------------------------------------------


class LineAmountCheck(BaseModel):
    """单行金额勾稽。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_no: int = Field(..., ge=1)
    expected_amount_excl_tax: NonNegativeMoney = Field(..., description="to_money(quantity × unit_price)")
    declared_amount_excl_tax: NonNegativeMoney
    expected_tax_amount: NonNegativeMoney = Field(..., description="to_money(declared_amount_excl_tax × tax_rate)")
    declared_tax_amount: NonNegativeMoney
    expected_amount_incl_tax: NonNegativeMoney = Field(..., description="declared_excl + declared_tax")
    declared_amount_incl_tax: NonNegativeMoney
    tax_rate: TaxRate
    tax_rate_allowed: bool
    amount_passed: bool
    tax_passed: bool


class InvoiceTotalCheck(BaseModel):
    """按发票号汇总的明细 vs 票面勾稽。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice_no: str
    line_nos: list[int] = Field(default_factory=list)
    lines_amount_incl_tax: NonNegativeMoney
    invoice_amount_incl_tax: PositiveMoney
    lines_tax_amount: NonNegativeMoney
    invoice_tax_amount: NonNegativeMoney
    amount_deviation: Money = Field(..., description="lines − invoice（含税）")
    tax_deviation: Money = Field(..., description="lines − invoice（税额）")
    passed: bool


class DuplicateInvoiceFlag(BaseModel):
    """重复发票标记。conflicting_claim_id 为 None 表示同一单据内部重复。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice_no: str
    conflicting_claim_id: str | None = None
    conflicting_status: InvoiceUsageStatus | None = None
    same_applicant: bool | None = Field(default=None, description="占用方是否同一申请人（仅供人工复核参考）")
    code: ViolationCode = Field(..., description="DUPLICATE_INVOICE_PAID / IN_FLIGHT / IN_SUBMISSION 之一")

    @model_validator(mode="after")
    def _check_code(self) -> DuplicateInvoiceFlag:
        allowed = {
            ViolationCode.DUPLICATE_INVOICE_PAID,
            ViolationCode.DUPLICATE_INVOICE_IN_FLIGHT,
            ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION,
        }
        if self.code not in allowed:
            raise ValueError("DuplicateInvoiceFlag.code 只能是重复发票类编码")
        internal = self.code is ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION
        if internal != (self.conflicting_claim_id is None):
            raise ValueError("IN_SUBMISSION 不得带 conflicting_claim_id；跨单重复必须带")
        return self


class SplitClaimEvidence(BaseModel):
    """疑似拆单规避审批证据（报销 / 合同共用）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: AwareDatetime
    window_end: AwareDatetime
    related_reference_ids: list[str] = Field(..., min_length=1, description="窗口内被合并计算的历史单据 / 合同 ID")
    window_total: PositiveMoney = Field(..., description="本单 + 相关单据合计")
    bypassed_threshold: PositiveMoney = Field(..., description="被规避的额度门槛（审批档上限或法务审查门槛）")
    single_amounts_below_threshold: bool = Field(..., description="每张单据各自均未超过门槛")
    escalated_tier: ApproverTier = Field(..., description="按 window_total 应当适用的审批层级")


class PaymentScheduleCheck(BaseModel):
    """合同付款计划勾稽。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_total: NonNegativeMoney
    subject_amount: PositiveMoney
    deviation: Money = Field(..., description="schedule_total − subject_amount")
    prepayment_amount: NonNegativeMoney = Field(..., description="due_in_days == 0 的分期合计")
    prepayment_ratio: Decimal = Field(..., ge=0, le=1, decimal_places=4, description="prepayment / subject，4 位小数")
    max_due_in_days: int = Field(..., ge=0)
    passed: bool


class AmountReconciliationResult(BaseModel):
    """确定性金额勾稽结果（Decimal，零浮点）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_type: AuditSubjectType
    declared_total: PositiveMoney = Field(..., description="报销：declared_total；合同：subject_amount")
    computed_total: NonNegativeMoney = Field(..., description="报销：明细含税合计；合同：付款计划合计（无计划时 == declared）")
    total_deviation: Money = Field(..., description="declared − computed")
    total_within_tolerance: bool
    line_checks: list[LineAmountCheck] = Field(default_factory=list)
    invoice_checks: list[InvoiceTotalCheck] = Field(default_factory=list)
    tax_check_passed: bool = Field(..., description="全部行税率在白名单且税额在容差内，且发票尾差合规")
    payment_schedule_check: PaymentScheduleCheck | None = None
    duplicate_invoice_flags: list[DuplicateInvoiceFlag] = Field(default_factory=list)
    has_duplicate_invoice: bool = False
    split_suspected: bool = Field(default=False, description="疑似拆单 / 拆分合同规避审批")
    split_evidence: SplitClaimEvidence | None = None
    approval_amount: PositiveMoney = Field(..., description="定档金额：max(本单金额, 拆单窗口合计)")
    approver_tier: ApproverTier
    passed: bool = Field(..., description="所有勾稽项通过且无重复、无拆单")

    @model_validator(mode="after")
    def _check_consistency(self) -> AmountReconciliationResult:
        if self.has_duplicate_invoice != bool(self.duplicate_invoice_flags):
            raise ValueError("has_duplicate_invoice 必须等价于 duplicate_invoice_flags 非空")
        if self.split_suspected != (self.split_evidence is not None):
            raise ValueError("split_suspected 必须等价于 split_evidence 非空")
        if self.total_deviation != self.declared_total - self.computed_total:
            raise ValueError("total_deviation 必须等于 declared_total − computed_total")
        is_contract = self.subject_type is AuditSubjectType.CONTRACT
        if is_contract and (self.line_checks or self.invoice_checks or self.duplicate_invoice_flags):
            raise ValueError("合同勾稽不得包含发票 / 明细检查")
        if not is_contract and self.payment_schedule_check is not None:
            raise ValueError("报销勾稽不得包含付款计划检查")
        if self.passed and (
            not self.total_within_tolerance
            or not self.tax_check_passed
            or self.has_duplicate_invoice
            or self.split_suspected
            or any(not c.amount_passed or not c.tax_passed or not c.tax_rate_allowed for c in self.line_checks)
            or any(not c.passed for c in self.invoice_checks)
            or (self.payment_schedule_check is not None and not self.payment_schedule_check.passed)
        ):
            raise ValueError("passed=True 但存在未通过的勾稽项")
        return self


# ---------------------------------------------------------------------------
# 合规风险
# ---------------------------------------------------------------------------


class ComplianceFinding(BaseModel):
    """单条合规风险。risk_level / min_route 不得低于 VIOLATION_POLICIES[code]；is_redline 必须与之相等。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(..., min_length=1, max_length=64, description="稳定 ID：f\"{code.value}:{序号}\"")
    code: ViolationCode
    risk_level: RiskLevel
    source: FindingSource
    min_route: ApprovalRoute
    is_redline: bool = False
    title: str = Field(..., min_length=1, max_length=120, description="确定性渲染的中文标题")
    detail: str = Field(..., min_length=1, max_length=1000, description="脱敏后说明（不得含 PII 原文）")
    rule_id: str | None = Field(default=None, max_length=32, description="RULE_ENGINE 来源必填，如 FIN-R03")
    policy_section: str | None = Field(default=None, max_length=200, description="制度章节提示，如 费用审批矩阵")
    citations: list[SopCitation] = Field(default_factory=list, description="制度条款引用（已通过闭锁校验）")
    citation_verified: bool = Field(default=False, description="citations 均已通过闭锁校验（及模型支撑性核验）")
    related_line_nos: list[int] = Field(default_factory=list)
    related_invoice_nos: list[str] = Field(default_factory=list)
    amount_delta: Money | None = Field(default=None, description="涉及的金额偏差（若有）")
    supplement_request: str | None = Field(default=None, max_length=300, description="NEEDS_SUPPLEMENT 时给申请人的补正要求")

    @model_validator(mode="after")
    def _check_policy(self) -> ComplianceFinding:
        policy = VIOLATION_POLICIES[self.code]
        if self.source not in policy.allowed_sources:
            raise ValueError(f"{self.code.value} 不允许来源 {self.source.value}")
        if self.risk_level.rank < policy.min_risk_level.rank:
            raise ValueError(f"{self.code.value} 风险等级不得低于 {policy.min_risk_level.value}")
        if self.min_route.rank < policy.min_route.rank:
            raise ValueError(f"{self.code.value} 路由不得低于 {policy.min_route.value}")
        if self.is_redline != policy.is_redline:
            raise ValueError("is_redline 必须与 VIOLATION_POLICIES 一致")
        if self.risk_level in MANUAL_REVIEW_RISK_LEVELS and self.min_route.rank < ApprovalRoute.ESCALATE_HUMAN.rank:
            raise ValueError("HIGH / CRITICAL finding 的 min_route 必须 >= ESCALATE_HUMAN")
        if self.min_route is ApprovalRoute.REJECT and self.source is not FindingSource.RULE_ENGINE:
            raise ValueError("只有 RULE_ENGINE 来源可以要求 REJECT")
        if self.source is FindingSource.RULE_ENGINE and not self.rule_id:
            raise ValueError("RULE_ENGINE 来源必须给出 rule_id")
        if self.citation_verified and not self.citations:
            raise ValueError("citation_verified=True 时 citations 不得为空")
        if self.min_route is ApprovalRoute.NEEDS_SUPPLEMENT and not self.supplement_request:
            raise ValueError("min_route == NEEDS_SUPPLEMENT 时必须给出 supplement_request")
        return self


class RuleEvaluation(BaseModel):
    """DeterministicFinanceRuleEngine.evaluate_* 的产物。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruleset_version: str
    reconciliation: AmountReconciliationResult
    findings: list[ComplianceFinding] = Field(default_factory=list, description="全部 source == RULE_ENGINE")
    approver_tier: ApproverTier = Field(..., description="== reconciliation.approver_tier（合同超门槛时为 LEGAL_AND_FINANCE_HEAD）")

    @model_validator(mode="after")
    def _check_source(self) -> RuleEvaluation:
        if any(f.source is not FindingSource.RULE_ENGINE for f in self.findings):
            raise ValueError("RuleEvaluation.findings 只能来自 RULE_ENGINE")
        return self


# ---------------------------------------------------------------------------
# 模型传输（FinanceReviewModelPort）
# ---------------------------------------------------------------------------


class FinanceModelResult(BaseModel, Generic[T]):
    """FinanceReviewModelPort 统一返回包装。"""

    output: T
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_name: str = Field(default="", description="实际调用的模型 ID，进入审计摘要")


class ModelRiskClaim(BaseModel):
    """模型提出的一条风险主张（尚未经闭锁引用校验）。不含路由字段；风险等级仅作参考，可被策略表抬高。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    code: ViolationCode = Field(..., description="必须属于 allowed_sources 含 MODEL_REVIEW 的编码，否则 agent 丢弃")
    risk_level: RiskLevel
    rationale: str = Field(..., min_length=1, max_length=600, description="基于脱敏文本的判断理由")
    evidence_excerpt: str = Field(default="", max_length=200, description="单据（脱敏后）原文片段，必须是视图文本子串")
    citation_parent_id: str | None = Field(default=None, max_length=192, description="支撑该主张的制度父块 ID")
    citation_quote: str = Field(default="", max_length=200, description="制度原文摘录，必须是该父块 full_content 子串")


class ExpenseReasonReviewDraft(BaseModel):
    """报销事由语义审查结构化输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: list[ModelRiskClaim] = Field(default_factory=list, max_length=20)
    reason_matches_categories: bool = Field(..., description="事由是否与费用类别 / 明细一致")
    injection_suspected: bool = Field(default=False, description="是否识别到试图操纵审批结论的指令性文本")
    summary: str = Field(default="", max_length=400, description="一句话审查摘要（脱敏文本）")


class ContractTermExtraction(BaseModel):
    """模型从条款原文抽取的关键条款事实（与结构化字段做确定性比对）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stated_payment_term_days: int | None = Field(default=None, ge=0, le=3650)
    stated_prepayment_ratio: Decimal | None = Field(default=None, ge=0, le=1, decimal_places=4)
    our_penalty_rate_text: str = Field(default="", max_length=120, description="我方违约金表述原文摘录")
    counterparty_penalty_rate_text: str = Field(default="", max_length=120)
    has_liability_cap: bool | None = None
    auto_renewal: bool | None = None
    exclusivity: bool | None = None


class ContractClauseReviewDraft(BaseModel):
    """合同条款语义审查结构化输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: list[ModelRiskClaim] = Field(default_factory=list, max_length=20)
    extracted_terms: ContractTermExtraction
    injection_suspected: bool = False
    summary: str = Field(default="", max_length=400)


class CitationSupportVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_index: int = Field(..., ge=0, description="对应输入 claims 的下标")
    supported: bool = Field(..., description="被引制度原文是否确实支撑该风险主张")
    reason: str = Field(default="", max_length=300)


class CitationSupportDraft(BaseModel):
    """引用支撑性核验结构化输出（只能否决、不能新增引用）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[CitationSupportVerdict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 人工审批（HumanApprovalGatewayPort）
# ---------------------------------------------------------------------------


class HumanApprovalTicketDraft(BaseModel):
    """人工复核工单草稿。summary 与 findings.detail 必须已通过 PiiRedactor.ensure_clean。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(
        ..., min_length=1, max_length=256, description="f\"{subject_type}:{reference_id}:{findings_fingerprint}\"，重试不重复建单"
    )
    subject_type: AuditSubjectType
    reference_id: str = Field(..., max_length=128, description="claim_id / contract_id")
    queue: ReviewerQueue
    max_risk_level: RiskLevel
    finding_codes: list[ViolationCode] = Field(..., min_length=1)
    findings: list[ComplianceFinding] = Field(..., min_length=1)
    approval_amount: PositiveMoney
    approver_tier: ApproverTier
    summary: str = Field(..., min_length=1, max_length=2000, description="脱敏审计摘要")
    requested_by_user_id: str = Field(..., max_length=128, description="取自 UserContext.user_id，不得取自模型输出")
    requested_by_dept_id: str = Field(..., max_length=64)
    trace_id: str | None = Field(default=None, max_length=256)
    sla_hours: int = Field(..., ge=1, le=720, description="CRITICAL 4h / HIGH 24h / 其余 72h（见 FinanceAuditConfig）")
    created_at: AwareDatetime


class HumanApprovalTicketReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1, max_length=128)
    queue: ReviewerQueue
    created_at: AwareDatetime
    deduplicated: bool = Field(default=False, description="True 表示命中 idempotency_key，返回的是已有工单")


# ---------------------------------------------------------------------------
# 编排中间结果
# ---------------------------------------------------------------------------


class PolicyRetrievalResult(BaseModel):
    """制度条款检索结果（RetrievalSystemPort.search 的薄包装）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(..., description="由脱敏视图与模板确定性构造的检索 query（不含 PII）")
    contexts: list[RetrievedParentChunk] = Field(default_factory=list, description="ACL 前置过滤后的父块（按排名）")
    degraded: bool = False
    degraded_reasons: list[DegradedReason] = Field(default_factory=list)
    unavailable: bool = Field(default=False, description="检索抛异常；contexts 必须为空")

    @model_validator(mode="after")
    def _check(self) -> PolicyRetrievalResult:
        if self.unavailable and self.contexts:
            raise ValueError("unavailable=True 时 contexts 必须为空")
        return self


class CitationCheckResult(BaseModel):
    """单条模型 claim 的闭锁引用校验结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_index: int = Field(..., ge=0)
    code: ViolationCode
    parent_id: str | None = None
    parent_in_contexts: bool = Field(..., description="parent_id 是否命中本轮送入模型的 contexts")
    quote_is_substring: bool = Field(..., description="citation_quote 是否为该父块 full_content 的子串（非空）")
    evidence_is_substring: bool = Field(..., description="evidence_excerpt 为空或为脱敏视图文本子串")
    support_verified: bool | None = Field(default=None, description="模型支撑性核验结论；未执行为 None")
    passed: bool
    citation: SopCitation | None = Field(default=None, description="passed 时由父块元数据确定性构造")

    @model_validator(mode="after")
    def _check(self) -> CitationCheckResult:
        deterministic = self.parent_in_contexts and self.quote_is_substring and self.evidence_is_substring
        if self.passed != (deterministic and self.support_verified is not False):
            raise ValueError("passed 必须等于 确定性校验通过 且 支撑性核验未否决")
        if self.passed != (self.citation is not None):
            raise ValueError("passed 时必须给出 citation，否则不得给出")
        return self


class ModelReviewOutcome(BaseModel):
    """模型审查 + 引用校验后的产物。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ModelReviewStatus
    findings: list[ComplianceFinding] = Field(default_factory=list, description="MODEL_REVIEW / SYSTEM_GUARD 来源")
    citation_checks: list[CitationCheckResult] = Field(default_factory=list)
    discarded_claim_count: int = Field(default=0, ge=0, description="引用未通过且风险 < HIGH 而被丢弃的 claim 数")
    model_names: list[str] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    @model_validator(mode="after")
    def _check(self) -> ModelReviewOutcome:
        if any(f.source is FindingSource.RULE_ENGINE for f in self.findings):
            raise ValueError("ModelReviewOutcome.findings 不得包含 RULE_ENGINE 来源")
        return self


class RouteDecision(BaseModel):
    """确定性审批路由合成结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: ApprovalRoute
    approver_tier: ApproverTier
    reviewer_queue: ReviewerQueue | None = Field(default=None, description="仅 ESCALATE_HUMAN 时给出")
    max_risk_level: RiskLevel | None = None
    decisive_finding_ids: list[str] = Field(default_factory=list, description="决定最终路由的 finding（min_route == route）")
    reasons: list[str] = Field(default_factory=list, description="确定性渲染的路由理由")

    @model_validator(mode="after")
    def _check(self) -> RouteDecision:
        if (self.route is ApprovalRoute.ESCALATE_HUMAN) != (self.reviewer_queue is not None):
            raise ValueError("reviewer_queue 仅且必须在 ESCALATE_HUMAN 时给出")
        return self


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class AuditTrailSummary(BaseModel):
    """脱敏审计摘要（写入审计日志 / 工单 / 响应；整体必须通过 PiiRedactor.ensure_clean）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_type: AuditSubjectType
    reference_id: str
    applicant_ref: str = Field(..., pattern=r"^app_[0-9a-f]{12}$", description="sha256(applicant.user_id) 前 12 位假名")
    store_code: str | None = None
    approval_amount: PositiveMoney
    route: ApprovalRoute
    approver_tier: ApproverTier
    finding_codes: list[ViolationCode] = Field(default_factory=list)
    max_risk_level: RiskLevel | None = None
    pii_redaction_counts: dict[PiiType, int] = Field(default_factory=dict)
    policy_refs: list[str] = Field(default_factory=list, description="f\"{doc_id}@v{version}#{section_path}\"")
    ruleset_version: str
    model_names: list[str] = Field(default_factory=list)
    summary_text: str = Field(..., min_length=1, max_length=1000)


class FinanceAuditResponse(BaseModel):
    """预审响应。四种路由均为 HTTP 200 的正常响应。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str | None = None
    subject_type: AuditSubjectType
    reference_id: str = Field(..., description="claim_id / contract_id")
    conclusion: str = Field(..., min_length=1, max_length=2000, description="预审结论（确定性模板渲染，脱敏）")
    route: ApprovalRoute
    approver_tier: ApproverTier
    reconciliation: AmountReconciliationResult
    findings: list[ComplianceFinding] = Field(default_factory=list, description="按 risk_level 降序、finding_id 升序")
    max_risk_level: RiskLevel | None = Field(default=None, description="findings 为空时为 None")
    supplement_requests: list[str] = Field(
        default_factory=list, description="补正清单（去重）；NEEDS_SUPPLEMENT 时必填，升级 / 驳回时可随附供人工参考"
    )
    citations: list[SopCitation] = Field(default_factory=list, description="全部 findings 引用去重（按 parent_id）")
    citation_check_passed: bool = Field(..., description="无任何未通过闭锁校验且被保留的模型 claim")
    model_review_status: ModelReviewStatus
    audit_summary: AuditTrailSummary
    human_approval_ticket_id: str | None = Field(default=None, description="ESCALATE_HUMAN 时必填")
    retrieval_degraded: bool = False
    latency_ms: float = Field(..., ge=0)
    stage_latency_ms: dict[AuditStage, float] = Field(default_factory=dict)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    token_cost: int = Field(default=0, ge=0, description="== token_usage.total_tokens（仅被测系统自身消耗）")

    @model_validator(mode="after")
    def _check_consistency(self) -> FinanceAuditResponse:
        if self.token_cost != self.token_usage.total_tokens:
            raise ValueError("token_cost 必须等于 token_usage.total_tokens")
        if self.reconciliation.subject_type is not self.subject_type:
            raise ValueError("reconciliation.subject_type 与 subject_type 不一致")
        if self.audit_summary.route is not self.route or self.audit_summary.reference_id != self.reference_id:
            raise ValueError("audit_summary 与响应路由 / 单据号不一致")
        if self.findings:
            if self.route.rank < max(f.min_route.rank for f in self.findings):
                raise ValueError("route 不得低于任一 finding 的 min_route")
            expected_max = max(self.findings, key=lambda f: f.risk_level.rank).risk_level
            if self.max_risk_level is not expected_max:
                raise ValueError("max_risk_level 必须等于 findings 中最高风险等级")
        elif self.max_risk_level is not None:
            raise ValueError("findings 为空时 max_risk_level 必须为 None")
        if self.route is ApprovalRoute.AUTO_APPROVE:
            blocked = any(f.is_redline or f.risk_level in MANUAL_REVIEW_RISK_LEVELS for f in self.findings)
            if blocked:
                raise ValueError("存在红线或 HIGH / CRITICAL 风险时严禁 AUTO_APPROVE")
            if not (self.reconciliation.passed and self.citation_check_passed):
                raise ValueError("AUTO_APPROVE 要求金额勾稽全部通过且引用校验通过")
            if self.model_review_status is not ModelReviewStatus.COMPLETED:
                raise ValueError("AUTO_APPROVE 要求模型语义审查已完成")
        if self.route is ApprovalRoute.REJECT and not any(
            f.source is FindingSource.RULE_ENGINE and f.code in REJECT_VIOLATION_CODES for f in self.findings
        ):
            raise ValueError("REJECT 必须由 RULE_ENGINE 的 REJECT_VIOLATION_CODES 触发")
        if (self.route is ApprovalRoute.ESCALATE_HUMAN) != (self.human_approval_ticket_id is not None):
            raise ValueError("human_approval_ticket_id 仅且必须在 ESCALATE_HUMAN 时给出")
        if self.route is ApprovalRoute.NEEDS_SUPPLEMENT and not self.supplement_requests:
            raise ValueError("NEEDS_SUPPLEMENT 必须给出 supplement_requests")
        return self


class FinanceAuditOutcome(BaseModel):
    """_run_pipeline 的内部产物：响应 + 评测映射所需的中间结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    response: FinanceAuditResponse
    rule_evaluation: RuleEvaluation
    policy_retrieval: PolicyRetrievalResult
    model_review: ModelReviewOutcome
    route_decision: RouteDecision
    ticket_receipt: HumanApprovalTicketReceipt | None = None


class FinanceAuditConfig(BaseModel):
    """FinanceAuditAgent 运行参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: FinanceRuleConfig = Field(default_factory=FinanceRuleConfig)
    allowed_caller_roles: frozenset[str] = Field(
        default=frozenset({"finance", "finance_manager", "legal"}),
        description="可调用预审的调用人角色（与《财务费用报销制度》ACL 一致）；不相交 → CallerForbiddenError",
    )
    policy_top_k: int = Field(default=5, ge=1, le=20, description="制度条款检索 top_k")
    ledger_timeout_s: float = Field(default=3.0, gt=0)
    retrieval_timeout_s: float = Field(default=5.0, gt=0)
    model_timeout_s: float = Field(default=20.0, gt=0)
    approval_timeout_s: float = Field(default=5.0, gt=0)
    verify_citation_support: bool = Field(default=True, description="是否调用模型做引用支撑性核验（确定性闭锁校验始终执行）")
    sla_hours_by_risk: dict[RiskLevel, int] = Field(
        default_factory=lambda: {RiskLevel.CRITICAL: 4, RiskLevel.HIGH: 24, RiskLevel.MEDIUM: 72, RiskLevel.LOW: 72}
    )
