"""财务预审智能体异常体系。

风格与 agent_matrix.supply_chain_bi.errors 一致：稳定 code、retryable、suggestion，to_hint() 输出可直接回灌给
LLM / 工作流的结构化修正提示；api.py 统一转为 data_platform ToolErrorResponse，禁止裸抛 500。

    异常                              HTTP   error_code                    retryable
    CallerForbiddenError              403    CALLER_FORBIDDEN              False   调用人非财务 / 法务角色
    SubmissionNotFoundError           404    SUBMISSION_NOT_FOUND          False   单据不存在或无权查看（不区分）
    InvalidAuditInvocationError       400    INVALID_AUDIT_INVOCATION      False   评测 question 不符合 FIN_AUDIT 引用格式
    LedgerUnavailableError            503    CLAIM_LEDGER_UNAVAILABLE      True    台账不可用（fail-closed，不出预审结论）
    HumanApprovalUnavailableError     503    HUMAN_APPROVAL_UNAVAILABLE    True    工单系统不可用（幂等，可安全重试）
    PiiLeakError                      503    PII_GUARD_TRIPPED             False   出口 PII 守卫拦截（缺陷，需人工排查）
    FinanceRuleConfigError            503    FINANCE_RULE_CONFIG_INVALID   False   规则配置自检失败（启动期）
    AgentNotInitializedError          503    AGENT_NOT_INITIALIZED         True
    UnsupportedScenarioError          400    UNSUPPORTED_SCENARIO          False

agent 内部消化、不向上抛出（转为 HTTP 200 + 路由兜底 ESCALATE_HUMAN）：
- FinanceModelUnavailableError / FinanceStructuredOutputError → ModelReviewStatus.MODEL_UNAVAILABLE
- RetrievalSystemPort 任意异常 → PolicyRetrievalResult.unavailable=True → ModelReviewStatus.NO_POLICY_CONTEXT

PII 铁律：任何异常的 message / suggestion / to_hint() 不得包含单据自由文本、银行卡号、身份证号、手机号；
PiiLeakError 只允许携带 PiiType 与出口位置（sink），绝不携带命中原文。
"""

from __future__ import annotations


class FinanceAuditError(Exception):
    """财务预审智能体异常基类。"""

    code: str = "FINANCE_AUDIT_ERROR"
    retryable: bool = False
    suggestion: str = "请检查请求参数后重试。"

    def __init__(self, message: str, *, trace_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.trace_id = trace_id

    def to_hint(self) -> str:
        """输出固定格式：`[{code}] trace_id={trace_id or '-'} | {message} | 建议: {suggestion}`。"""
        from agent_matrix.finance_audit.redaction import PiiRedactor
        redactor = PiiRedactor()
        hint = f"[{self.code}] trace_id={self.trace_id or '-'} | {self.message} | 建议: {self.suggestion}"
        return redactor.redact_text(hint).text


class CallerForbiddenError(FinanceAuditError):
    code = "CALLER_FORBIDDEN"
    suggestion = "财务预审仅对财务 / 财务经理 / 法务角色开放，请由财务岗位发起预审。"


class SubmissionNotFoundError(FinanceAuditError):
    code = "SUBMISSION_NOT_FOUND"
    suggestion = "未找到该单据或当前身份无权查看，请核对单据号（claim_id / contract_id）。"


class InvalidAuditInvocationError(FinanceAuditError):
    code = "INVALID_AUDIT_INVOCATION"
    suggestion = "评测问题须为 FIN_AUDIT:{expense|contract}:{reference_id} 格式。"


class LedgerUnavailableError(FinanceAuditError):
    code = "CLAIM_LEDGER_UNAVAILABLE"
    retryable = True
    suggestion = "历史报销台账暂不可用，无法排除重复发票与拆单风险，已暂停预审，请稍后重试。"


class FinanceModelUnavailableError(FinanceAuditError):
    """仅由 FinanceReviewModelPort 抛出，agent 内部消化为 MODEL_UNAVAILABLE + ESCALATE_HUMAN。"""

    code = "FINANCE_MODEL_UNAVAILABLE"
    retryable = True
    suggestion = "语义审查模型暂不可用，单据已转人工复核。"


class FinanceStructuredOutputError(FinanceAuditError):
    """仅由 FinanceReviewModelPort 抛出，agent 内部消化为 MODEL_UNAVAILABLE + ESCALATE_HUMAN。"""

    code = "STRUCTURED_OUTPUT_INVALID"
    retryable = True
    suggestion = "模型输出不符合约定 schema，单据已转人工复核。"


class HumanApprovalUnavailableError(FinanceAuditError):
    code = "HUMAN_APPROVAL_UNAVAILABLE"
    retryable = True
    suggestion = "人工审批工单系统暂不可用；工单按幂等键创建，重试不会重复建单，请稍后重试。"


class PiiLeakError(FinanceAuditError):
    """出口 PII 守卫拦截。构造时只传 PiiType 名称与 sink（如 model_input / log / response / ticket）。"""

    code = "PII_GUARD_TRIPPED"
    suggestion = "检测到待输出内容含未脱敏隐私信息，已阻断输出，请联系平台管理员排查脱敏规则。"


class FinanceRuleConfigError(FinanceAuditError):
    code = "FINANCE_RULE_CONFIG_INVALID"
    suggestion = "财务规则配置自检失败（审批档位 / 类别规则 / 违规策略表不一致），请修正 FinanceRuleConfig。"


class AgentNotInitializedError(FinanceAuditError):
    code = "AGENT_NOT_INITIALIZED"
    retryable = True
    suggestion = "智能体服务正在启动或未完成装配，请稍后重试。"


class UnsupportedScenarioError(FinanceAuditError):
    code = "UNSUPPORTED_SCENARIO"
    suggestion = "FinanceAuditAgent 仅支持 EvalScenario.FINANCE_APPROVAL，请检查评测样本 scenario。"
