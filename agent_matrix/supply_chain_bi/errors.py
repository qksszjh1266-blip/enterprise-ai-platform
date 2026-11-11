"""供应链 BI 智能体异常体系。

风格与 agent_matrix.store_sop.errors 一致：稳定 code、retryable、suggestion，to_hint() 输出可直接回灌给
LLM / 工作流的结构化修正提示。

    异常                          HTTP   error_code                 retryable
    ScopeDeniedError              403    SCOPE_FORBIDDEN            False   身份无任何仓库 / 大区授权
    ScopeResolutionError          503    SCOPE_RESOLUTION_FAILED    True    权限系统不可用（fail-closed，绝不降级为全国）
    SqlModelUnavailableError      503    SQL_MODEL_UNAVAILABLE      True
    StructuredOutputError         502    STRUCTURED_OUTPUT_INVALID  True
    SemanticCatalogError          503    SEMANTIC_CATALOG_INVALID   False   目录配置自检失败（启动期）
    AgentNotInitializedError      503    AGENT_NOT_INITIALIZED      True
    UnsupportedScenarioError      400    UNSUPPORTED_SCENARIO       False

注意（不是异常）：
- 守卫违规、SQL 语法 / 列名错误 → SqlValidationResult / SqlExecutionResult 的 error_hint，进入自纠错循环；
- 重试耗尽 → StopReason.SQL_RETRY_EXHAUSTED；沙箱不可用（SandboxUnavailableError）→ agent 内部捕获后
  转为 StopReason.DATA_SOURCE_UNAVAILABLE 的 HTTP 200 响应，不向上抛出。
"""

from __future__ import annotations


class SupplyChainAgentError(Exception):
    """供应链 BI 智能体异常基类。"""

    code: str = "SUPPLY_CHAIN_AGENT_ERROR"
    retryable: bool = False
    suggestion: str = "请检查请求参数后重试。"

    def __init__(self, message: str, *, trace_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.trace_id = trace_id

    def to_hint(self) -> str:
        """输出固定格式：`[{code}] trace_id={trace_id or '-'} | {message} | 建议: {suggestion}`。"""
        return f"[{self.code}] trace_id={self.trace_id or '-'} | {self.message} | 建议: {self.suggestion}"


class ScopeDeniedError(SupplyChainAgentError):
    code = "SCOPE_FORBIDDEN"
    suggestion = "当前身份未被授予任何仓库或大区的数据权限，请联系供应链数据管理员开通。"


class ScopeResolutionError(SupplyChainAgentError):
    code = "SCOPE_RESOLUTION_FAILED"
    retryable = True
    suggestion = "权限系统暂不可用，为防止越权已拒绝执行查询，请稍后重试。"


class SqlModelUnavailableError(SupplyChainAgentError):
    code = "SQL_MODEL_UNAVAILABLE"
    retryable = True
    suggestion = "分析模型服务暂不可用，请稍后重试。"


class StructuredOutputError(SupplyChainAgentError):
    code = "STRUCTURED_OUTPUT_INVALID"
    retryable = True
    suggestion = "模型输出不符合约定 schema，已终止本次分析，请重试。"


class SandboxUnavailableError(SupplyChainAgentError):
    """只读沙箱基础设施故障（连接失败 / 连接池耗尽）。仅由 ReadOnlySqlSandboxPort 抛出，agent 内部消化。"""

    code = "DATA_SOURCE_UNAVAILABLE"
    retryable = True
    suggestion = "分析数据源暂不可用，请稍后重试；紧急补货请按线下预警流程处理。"


class SemanticCatalogError(SupplyChainAgentError):
    code = "SEMANTIC_CATALOG_INVALID"
    suggestion = "语义目录自检失败（内置预警模板未通过守卫或引用了未登记的表列），请修正 semantic_layer 配置。"


class AgentNotInitializedError(SupplyChainAgentError):
    code = "AGENT_NOT_INITIALIZED"
    retryable = True
    suggestion = "智能体服务正在启动或未完成装配，请稍后重试。"


class UnsupportedScenarioError(SupplyChainAgentError):
    code = "UNSUPPORTED_SCENARIO"
    suggestion = "SupplyChainBIAgent 仅支持 EvalScenario.SUPPLY_CHAIN，请检查评测样本 scenario。"
