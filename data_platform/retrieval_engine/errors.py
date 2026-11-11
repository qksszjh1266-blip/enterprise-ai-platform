"""检索读链路异常体系。

与 storage_sync.errors 风格一致：每个异常携带稳定 code、retryable 与 suggestion，
由 mcp_server 的异常处理器转换为 ToolErrorResponse，禁止裸抛 500。

注意：单路召回 / 精排失败属于"降级"而非异常，不抛出，仅在 SearchResponse 中标记。
"""

from __future__ import annotations


class RetrievalError(Exception):
    """检索层异常基类。"""

    code: str = "RETRIEVAL_ERROR"
    retryable: bool = False
    suggestion: str = "请检查请求参数后重试。"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_hint(self) -> str:
        """生成结构化修正提示文本。

        格式：[{code}] {message} | 建议: {suggestion}
        """
        return f"[{self.code}] {self.message} | 建议: {self.suggestion}"


class RetrievalUnavailableError(RetrievalError):
    """向量与 BM25 两路召回均失败（含超时），无法给出任何结果。"""

    code = "RETRIEVAL_UNAVAILABLE"
    retryable = True
    suggestion = "检索服务暂时不可用，请稍后重试；不要基于空结果回答用户问题。"


class FilterTooBroadError(RetrievalError):
    """ACL 可见文档数超过 RetrievalConfig.max_filter_docs，无法安全下推过滤条件。"""

    code = "FILTER_TOO_BROAD"
    retryable = False
    suggestion = "可见文档范围过大，请联系管理员收敛角色权限或按业务域拆分知识库。"


class LedgerUnavailableError(RetrievalError):
    """读取 kb_documents 台账失败，无法构造 ACL / 版本过滤条件（fail-closed，不做无过滤检索）。"""

    code = "LEDGER_UNAVAILABLE"
    retryable = True
    suggestion = "权限台账暂时不可用，请稍后重试。"
