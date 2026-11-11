"""Acceptance #76–80 and fail-closed HTTP boundaries."""
import logging
import pytest
from httpx import AsyncClient, ASGITransport
from agent_matrix.finance_audit.api import create_app, API_PREFIX
from agent_matrix.finance_audit.errors import *
from agent_matrix.finance_audit.models import FinanceAuditResponse
from data_platform.retrieval_engine.models import ToolErrorResponse
from tests.agent_matrix.finance_audit.conftest import FINANCE,CLERK,submission
from tests.agent_matrix.finance_audit.test_agent_loop import LeakyPhoneRedactor

HEADERS={'X-User-Id':FINANCE.user_id,'X-Dept-Id':FINANCE.dept_id,'X-User-Roles':','.join(FINANCE.roles)}


def client(agent=None,**kwargs):
    return AsyncClient(transport=ASGITransport(app=create_app(agent,**kwargs),raise_app_exceptions=False),base_url='http://test')


def payload(ledger):
    return {'submission':submission(ledger,'E01').model_dump(mode='json')}


def test_76_openapi():
    api=create_app().openapi()
    assert api['paths'][API_PREFIX+'/audit']['post']['operationId']=='finance_audit_submit'
    assert api['paths'][API_PREFIX+'/rule-limits']['get']['operationId']=='finance_audit_get_rule_limits'
    assert {'FinanceAuditPayload','FinanceAuditResponse','ExpenseAuditSubmission','ContractAuditSubmission','ToolErrorResponse'}<=set(api['components']['schemas'])


async def test_77_identity_override_and_body_fallback(agent,ledger,monkeypatch):
    body=payload(ledger); body['user_context']=CLERK.model_dump()
    seen=[]; original=agent.audit
    async def audit(request):
        seen.append(request.user_context)
        return await original(request)
    monkeypatch.setattr(agent,'audit',audit)
    async with client(agent) as c:
        r=await c.post(API_PREFIX+'/audit',json=body,headers=HEADERS)
        assert r.status_code==200 and seen[-1]==FINANCE
        assert FinanceAuditResponse.model_validate(r.json())
        r=await c.post(API_PREFIX+'/audit',json=body)
        assert r.status_code==401
    async with client(agent,allow_body_identity=True) as c:
        body['user_context']=FINANCE.model_dump()
        assert (await c.post(API_PREFIX+'/audit',json=body)).status_code==200
        assert (await c.post(API_PREFIX+'/audit',json=body,headers={'X-User-Id':'partial'})).status_code==401


@pytest.mark.parametrize('bad',['number','bank','extra'])
async def test_77_validation_no_input_echo(agent,ledger,bad):
    body=payload(ledger)
    if bad=='number': body['submission']['declared_total']=128.505; secret='128.505'
    elif bad=='bank':
        secret='622202123456789012312345678901234567890'; body['submission']['payee_bank_account']=secret
    else:
        secret='13800138000'; body['submission'][secret]='x'
    async with client(agent) as c:
        r=await c.post(API_PREFIX+'/audit',json=body,headers=HEADERS)
    assert r.status_code==422 and secret not in r.text
    error=ToolErrorResponse.model_validate(r.json())
    assert error.details and '字符串' in error.hint


@pytest.mark.parametrize('error,status',[(CallerForbiddenError,403),(SubmissionNotFoundError,404),
    (FinanceStructuredOutputError,502),(LedgerUnavailableError,503),(HumanApprovalUnavailableError,503),
    (PiiLeakError,503),(InvalidAuditInvocationError,400),(UnsupportedScenarioError,400)])
async def test_78_error_mapping(agent,ledger,monkeypatch,error,status):
    async def audit(req): raise error('可安全重试的测试错误')
    monkeypatch.setattr(agent,'audit',audit)
    async with client(agent) as c:
        r=await c.post(API_PREFIX+'/audit',json=payload(ledger),headers=HEADERS)
    assert r.status_code==status
    assert ToolErrorResponse.model_validate(r.json()).error_code==error.code


async def test_78_not_initialized_and_forbidden(agent,ledger):
    async with client() as c:
        r=await c.post(API_PREFIX+'/audit',json=payload(ledger),headers=HEADERS)
        assert r.status_code==503 and ToolErrorResponse.model_validate(r.json()).error_code=='AGENT_NOT_INITIALIZED'
    async with client(agent) as c:
        r=await c.post(API_PREFIX+'/audit',json=payload(ledger),headers={**HEADERS,'X-User-Roles':'store_clerk'})
        assert r.status_code==403 and ToolErrorResponse.model_validate(r.json()).error_code=='CALLER_FORBIDDEN'


async def test_79_unexpected_no_secrets(agent,ledger,monkeypatch,caplog):
    async def audit(req): raise RuntimeError('boom 6222021234567890123 /secret/path')
    monkeypatch.setattr(agent,'audit',audit)
    with caplog.at_level(logging.ERROR):
        async with client(agent) as c:
            r=await c.post(API_PREFIX+'/audit',json=payload(ledger),headers=HEADERS)
    assert r.status_code==503 and ToolErrorResponse.model_validate(r.json()).error_code=='INTERNAL_ERROR'
    assert all(v not in r.text+caplog.text for v in ('boom','6222021234567890123','/secret/path','Traceback'))


async def test_80_rule_limits(agent):
    async with client(agent) as c:
        r=await c.get(API_PREFIX+'/rule-limits',headers=HEADERS)
        assert r.status_code==200
        body=r.json()
        assert body['approval_bands'][0]['upper_inclusive']=='500'
        assert all(isinstance(v,str) for v in body['allowed_tax_rates'])
        assert 'prompt_injection_markers' not in body
        assert (await c.get(API_PREFIX+'/rule-limits')).status_code==401
        assert (await c.get(API_PREFIX+'/rule-limits',headers={**HEADERS,'X-User-Roles':','})).status_code==401


async def test_75_http_pii_guard(agent,ledger):
    agent.redactor=LeakyPhoneRedactor()
    async with client(agent) as c:
        r=await c.post(API_PREFIX+'/audit',json=payload(ledger),headers=HEADERS)
    assert r.status_code==503 and ToolErrorResponse.model_validate(r.json()).error_code=='PII_GUARD_TRIPPED'
    assert '13800138000' not in r.text and not agent.review_model.calls
