"""财务预审智能体 FastAPI 路由。

    POST /api/v1/agents/finance-audit/audit         -> FinanceAuditAgent.audit（报销单 / 合同，按 submission.subject_type 判别）
    GET  /api/v1/agents/finance-audit/rule-limits   -> 当前生效的审批档位 / 类别额度 / 税率白名单 / 违规策略表（不含任何单据数据）

身份注入：与 电商运营 SOP / 供应链 BI 相同——网关注入 `X-User-Id` / `X-Dept-Id` / `X-User-Roles`（逗号分隔），
get_trusted_user_context 据此构造 UserContext 并**覆盖** FinanceAuditPayload.user_context；三个头都缺失时
仅在 app.state.allow_body_identity=True（内网调试）下回退使用请求体，否则 401。
submission.applicant 是单据业务事实，**永远不**作为调用身份或 ACL 依据。

金额输入：Money / TaxRate / Quantity 拒绝 JSON 数字（float），必须以字符串传入（"128.50"）；违者 422，
details 中 loc 指向具体字段，hint 提示"金额请以字符串传入"。

工具错误处理约定：任何异常都转为 data_platform ToolErrorResponse，禁止裸抛 500。

    异常                              HTTP   error_code                    retryable
    RequestValidationError            422    INVALID_ARGUMENT              False
    身份头缺失 / 非法                   401    UNAUTHENTICATED               False
    FinanceAuditError 子类             见 errors.py 模块文档映射表         {exc.retryable}
    未预期 Exception                   503    INTERNAL_ERROR                True   (日志不记录异常文本，响应不泄漏)

四种审批路由（AUTO_APPROVE / NEEDS_SUPPLEMENT / ESCALATE_HUMAN / REJECT）均为 HTTP 200 的正常 FinanceAuditResponse。

PII 铁律：422 的 details[*].msg 使用固定文案，**不得**回显 input 值（Pydantic 默认错误含 input，可能是银行卡号）；
INTERNAL_ERROR 响应不含异常消息、堆栈、类名、文件路径与任何单据片段。

注意：本模块不使用 `from __future__ import annotations`，避免 FastAPI 依赖解析时的前向引用问题。
"""

from agent_matrix.finance_audit.errors import AgentNotInitializedError
from agent_matrix.finance_audit.redaction import PiiRedactor
from agent_matrix.finance_audit.models import (
    FinanceAuditRequest, ExpenseAuditSubmission, ContractAuditSubmission, ApplicantProfile,
    InvoiceInfo, ExpenseLineItem, CounterpartyInfo, PaymentInstallment,
)
import logging

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_matrix.finance_audit.agent import FinanceAuditAgent
from agent_matrix.finance_audit.errors import FinanceAuditError
from agent_matrix.finance_audit.models import (
    FinanceAuditPayload,
    FinanceAuditResponse,
    FinanceRuleConfig,
)
from data_platform.retrieval_engine.models import ToolErrorResponse, UserContext

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/agents/finance-audit"

ERROR_STATUS_BY_CODE: dict[str, int] = {
    "UNAUTHENTICATED": 401,
    "CALLER_FORBIDDEN": 403,
    "SUBMISSION_NOT_FOUND": 404,
    "INVALID_AUDIT_INVOCATION": 400,
    "CLAIM_LEDGER_UNAVAILABLE": 503,
    "FINANCE_MODEL_UNAVAILABLE": 503,
    "STRUCTURED_OUTPUT_INVALID": 502,
    "HUMAN_APPROVAL_UNAVAILABLE": 503,
    "PII_GUARD_TRIPPED": 503,
    "FINANCE_RULE_CONFIG_INVALID": 503,
    "AGENT_NOT_INITIALIZED": 503,
    "UNSUPPORTED_SCENARIO": 400,
}
"""FinanceAuditError.code → HTTP 状态码；未登记的 code 一律 400。"""

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ToolErrorResponse, "description": "缺少可信身份头"},
    403: {"model": ToolErrorResponse, "description": "调用人非财务 / 法务角色"},
    404: {"model": ToolErrorResponse, "description": "单据不存在或无权查看"},
    422: {"model": ToolErrorResponse, "description": "请求体校验失败（含金额以浮点数传入），details 列出字段级错误"},
    502: {"model": ToolErrorResponse, "description": "模型结构化输出非法，可重试"},
    503: {"model": ToolErrorResponse, "description": "台账 / 工单系统 / 模型暂不可用或出口隐私守卫拦截"},
}

router = APIRouter(prefix=API_PREFIX, tags=["finance-audit-agent"])


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------


def get_finance_audit_agent(request: Request) -> FinanceAuditAgent:
    """从 request.app.state.finance_audit_agent 取出共享实例；为 None 时抛 AgentNotInitializedError（→ 503 结构化）。"""
    agent = getattr(request.app.state, 'finance_audit_agent', None)
    if agent is None:
        raise AgentNotInitializedError('服务未初始化')
    return agent


def get_trusted_user_context(request: Request, body_user_context: UserContext | None) -> UserContext:
    """规则同 agent_matrix.supply_chain_bi.api.get_trusted_user_context（不得 import 复用其私有符号，需在本模块定义
    code="UNAUTHENTICATED" 的 FinanceAuditError 子类）：头部齐全 → 构造并覆盖；全缺失且 allow_body_identity → body；
    其余（部分缺失 / 非法 / 空角色）→ 401。"""
    values = [request.headers.get(name) for name in ('X-User-Id', 'X-Dept-Id', 'X-User-Roles')]
    if all(v is None for v in values) and request.app.state.allow_body_identity and body_user_context is not None:
        return body_user_context
    if not all(v and v.strip() for v in values):
        raise _UnauthenticatedError('缺少完整可信身份头')
    try:
        return UserContext(user_id=values[0].strip(), dept_id=values[1].strip(),
            roles=[r.strip() for r in values[2].split(',') if r.strip()])
    except ValueError:
        raise _UnauthenticatedError('可信身份头格式无效') from None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post(
    "/audit",
    operation_id="finance_audit_submit",
    summary="费用报销 / 商品采购合同合规预审",
    description=(
        "提交一张部门费用报销单（subject_type=expense）或一份商品采购 / 仓储物流合同（subject_type=contract）进行合规预审。"
        "金额、税率、数量必须以字符串传入（如 \"128.50\"、\"0.13\"）。"
        "route=AUTO_APPROVE 表示预审通过、按 approver_tier 进入常规审批（不等于付款）；"
        "NEEDS_SUPPLEMENT 时 supplement_requests 为补正清单；ESCALATE_HUMAN 时 human_approval_ticket_id 为人工复核单号；"
        "REJECT 表示确定性规则证实违规（如重复发票已支付）。所有输出均已脱敏。"
    ),
    response_model=FinanceAuditResponse,
    responses=_ERROR_RESPONSES,
)
async def submit_audit(body: FinanceAuditPayload, request: Request) -> FinanceAuditResponse:
    """user_context = get_trusted_user_context(request, body.user_context)；
    构造 FinanceAuditRequest(submission, user_context, trace_id) 后调用 agent.audit。"""
    user = get_trusted_user_context(request, body.user_context)
    return await get_finance_audit_agent(request).audit(FinanceAuditRequest(
        submission=body.submission, user_context=user, trace_id=body.trace_id))


@router.get(
    "/rule-limits",
    operation_id="finance_audit_get_rule_limits",
    summary="读取当前生效的财务预审规则口径（审批档位 / 类别额度 / 税率白名单）",
    response_model=FinanceRuleConfig,
    response_model_exclude={"prompt_injection_markers"},
    responses=_ERROR_RESPONSES,
)
async def get_rule_limits(request: Request) -> FinanceRuleConfig:
    """需可信身份（与 submit_audit 同规则，且调用人角色须通过 agent 的 allowed_caller_roles 校验）；
    返回 agent.config.rules。prompt_injection_markers 不对外暴露（公开即等于提供绕过清单）。"""
    user = get_trusted_user_context(request, None)
    agent = get_finance_audit_agent(request)
    agent._authorize_caller(user, trace_id=None)
    return agent.config.rules


# ---------------------------------------------------------------------------
# 异常处理器（全部返回 ToolErrorResponse）
# ---------------------------------------------------------------------------


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 INVALID_ARGUMENT；details=[{"loc": "body.submission.lines.0.unit_price", "msg": 固定文案}]，
    **不回显 input**；任一错误源自 _reject_binary_float 时 hint 追加"金额 / 税率 / 数量请以字符串传入"。"""
    # Even validation locations can contain attacker-controlled extra field names.
    known = set(FinanceAuditPayload.model_fields) | set(FinanceAuditRequest.model_fields)
    for schema in (ExpenseAuditSubmission, ContractAuditSubmission, ApplicantProfile, InvoiceInfo,
                   ExpenseLineItem, CounterpartyInfo, PaymentInstallment, UserContext):
        known.update(schema.model_fields)
    known.update({'body', 'expense', 'contract'})
    details = [{'loc': '.'.join(str(p) if isinstance(p, int) or p in known else 'field' for p in e['loc']),
                'msg': '字段格式无效，请按接口契约修正'} for e in exc.errors()]
    body = ToolErrorResponse(error_code='INVALID_ARGUMENT', message='请求参数校验失败',
        hint='请核对字段格式；金额 / 税率 / 数量请以字符串传入。', details=details)
    return JSONResponse(status_code=422, content=body.model_dump(mode='json'))


async def handle_finance_audit_error(request: Request, exc: FinanceAuditError) -> JSONResponse:
    """按 ERROR_STATUS_BY_CODE 映射状态码；error_code=exc.code，hint=exc.to_hint()，retryable=exc.retryable。"""
    redactor = PiiRedactor()
    body = ToolErrorResponse(error_code=exc.code, message=redactor.redact_text(exc.message).text,
        hint=exc.to_hint(), retryable=exc.retryable)
    return JSONResponse(status_code=ERROR_STATUS_BY_CODE.get(exc.code, 400), content=body.model_dump(mode='json'))


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """503 INTERNAL_ERROR；只记录固定文案日志（不记录异常文本：上游异常可能夹带单据原文 / PII）；
    响应体不含异常消息、堆栈、类名、文件路径与任何单据片段。"""
    logger.error('财务预审发生内部错误，输出已阻断')
    body = ToolErrorResponse(error_code='INTERNAL_ERROR', message='服务暂不可用',
        hint='[INTERNAL_ERROR] 请稍后重试或联系平台管理员。', retryable=True)
    return JSONResponse(status_code=503, content=body.model_dump(mode='json'))


def create_app(agent: FinanceAuditAgent | None = None, *, allow_body_identity: bool = False) -> FastAPI:
    """装配应用：挂载 router、注册三个异常处理器、写入 app.state（装配已就位，仅为使 openapi() 可导出）。

    agent 为 None 时仍可调用 create_app().openapi() 导出 Schema（供平台 Workflow 导入）。
    """
    app = FastAPI(
        title="Finance Audit Agent API",
        version="0.5.0",
        description="综合电商费用报销与商品采购合同合规预审：PII 确定性脱敏、Decimal 金额勾稽、制度引用闭锁校验、高风险转人工复核。",
    )
    app.state.finance_audit_agent = agent
    app.state.allow_body_identity = allow_body_identity
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(FinanceAuditError, handle_finance_audit_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


class _UnauthenticatedError(FinanceAuditError):
    code = 'UNAUTHENTICATED'
    suggestion = '请通过可信网关提供完整用户身份。'
