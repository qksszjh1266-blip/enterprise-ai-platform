"""Acceptance #67–75: real hybrid retrieval and deterministic offline ports."""
import asyncio
import logging
from decimal import Decimal
from time import perf_counter
import pytest
from agent_matrix.finance_audit.models import *
from agent_matrix.finance_audit.errors import *
from agent_matrix.finance_audit.redaction import PiiRedactor
from eval_platform.models import AgentInvocation, EvalScenario
from tests.agent_matrix.finance_audit.conftest import FINANCE, LEGAL, MANAGER, CLERK, request, submission
from tests.agent_matrix.finance_audit.build_data import PII, ID, CARD2

EXPECTED={
 'E01':('AUTO_APPROVE',set()),'E02':('AUTO_APPROVE',set()),'E03':('AUTO_APPROVE',set()),
 'E04':('NEEDS_SUPPLEMENT',{'FIN_TAX_AMOUNT_MISMATCH'}),
 'E05':('NEEDS_SUPPLEMENT',{'FIN_DECLARED_TOTAL_MISMATCH'}),
 'E06':('REJECT',{'FIN_DUPLICATE_INVOICE_PAID'}),
 'E07':('ESCALATE_HUMAN',{'FIN_DUPLICATE_INVOICE_IN_FLIGHT'}),
 'E08':('AUTO_APPROVE',set()),'E09':('ESCALATE_HUMAN',{'FIN_SUSPECTED_SPLIT_CLAIM'}),
 'E10':('AUTO_APPROVE',set()),'E11':('ESCALATE_HUMAN',{'FIN_CATEGORY_LIMIT_EXCEEDED'}),
 'E12':('NEEDS_SUPPLEMENT',{'FIN_MISSING_ATTACHMENT'}),'E13':('NEEDS_SUPPLEMENT',{'FIN_LODGING_OVER_CAP'}),
 'E14':('ESCALATE_HUMAN',{'FIN_SUBMISSION_OVERDUE'}),'E15':('ESCALATE_HUMAN',{'FIN_SELF_REVIEW_CONFLICT'}),
 'E16':('AUTO_APPROVE',set()),'E17':('ESCALATE_HUMAN',{'SEC_PROMPT_INJECTION_SUSPECTED'}),
 'C01':('AUTO_APPROVE',set()),'C02':('ESCALATE_HUMAN',{'CTR_PAYMENT_SCHEDULE_MISMATCH','CTR_LEGAL_REVIEW_REQUIRED'}),
 'C03':('ESCALATE_HUMAN',{'CTR_PAYMENT_TERM_EXCEEDED'}),'C04':('NEEDS_SUPPLEMENT',{'CTR_BREACH_CLAUSE_MISSING'}),
 'C05':('ESCALATE_HUMAN',{'CTR_UNLIMITED_LIABILITY'}),'C06':('ESCALATE_HUMAN',{'CTR_SUSPECTED_SPLIT_CONTRACT'}),
 'C07':('AUTO_APPROVE',set()),'C08':('NEEDS_SUPPLEMENT',{'CTR_CLAUSE_FIELD_MISMATCH'}),
}


@pytest.mark.parametrize('ref',EXPECTED)
async def test_67_all_seed_routes(agent,ledger,ref):
    result=await agent.audit(request(ledger,ref))
    route,codes=EXPECTED[ref]
    assert result.route.value==route
    assert {f.code.value for f in result.findings}==codes
    assert result.token_cost==result.token_usage.total_tokens==sum(agent.review_model.token_usage.total_tokens for _ in agent.review_model.calls)
    if ref in {'E01','E10'}: assert result.approver_tier is ApproverTier.STORE_MANAGER
    if ref=='E02': assert result.approver_tier is ApproverTier.REGIONAL_MANAGER
    if ref=='E11': assert result.approver_tier is ApproverTier.REGIONAL_MANAGER_AND_FINANCE
    if ref=='C01': assert result.approver_tier is ApproverTier.REGIONAL_HEAD_AND_FINANCE_HEAD
    if ref=='C02':
        assert result.approver_tier is ApproverTier.LEGAL_AND_FINANCE_HEAD
        assert agent.approval_gateway.drafts[0].queue is ReviewerQueue.FINANCE_AND_LEGAL
    if ref=='C03': assert agent.approval_gateway.drafts[0].queue is ReviewerQueue.LEGAL_COUNSEL
    if ref=='E06': assert not agent.review_model.calls and result.token_cost==0
    if ref=='E04': assert result.findings[0].amount_delta==Decimal('.05')
    if ref=='E13': assert result.findings[0].amount_delta==Decimal('140')
    if ref=='E12': assert '报价单' in ' '.join(result.supplement_requests)
    if ref=='E09':
        evidence=result.reconciliation.split_evidence
        assert evidence.window_total==result.reconciliation.approval_amount==Decimal('1430')
        assert set(evidence.related_reference_ids)=={'HIST-D2','HIST-D4'}
        assert evidence.escalated_tier is ApproverTier.REGIONAL_MANAGER_AND_FINANCE
        assert result.findings[0].citations[0].doc_id=='finance_matrix'
    if ref=='C06': assert result.reconciliation.split_evidence.bypassed_threshold==Decimal('50000')
    if ref=='C05': assert result.findings[0].citation_verified


@pytest.mark.parametrize('fail',['timeout','unavailable','invalid'])
async def test_68_model_degradation(agent,ledger,fail):
    agent.review_model.fail=fail
    r=await agent.audit(request(ledger))
    assert r.route is ApprovalRoute.ESCALATE_HUMAN
    assert r.model_review_status is ModelReviewStatus.MODEL_UNAVAILABLE
    assert r.findings[0].code is ViolationCode.MODEL_REVIEW_UNAVAILABLE


async def test_68_ledger_fail_cancels_retrieval(agent,ledger,monkeypatch):
    cancelled=asyncio.Event()
    async def search(req):
        try: await asyncio.sleep(30)
        finally: cancelled.set()
    monkeypatch.setattr(agent.retrieval,'search',search)
    ledger.fail=True
    with pytest.raises(LedgerUnavailableError): await agent.audit(request(ledger))
    assert cancelled.is_set()
    assert not agent.review_model.calls


@pytest.mark.parametrize('timeout',[False,True])
async def test_68_retrieval_degradation(agent,ledger,monkeypatch,timeout):
    async def search(req):
        if timeout: await asyncio.sleep(30)
        raise RuntimeError('unavailable')
    monkeypatch.setattr(agent.retrieval,'search',search)
    agent.config=agent.config.model_copy(update={'retrieval_timeout_s':.01})
    r=await agent.audit(request(ledger))
    assert r.route is ApprovalRoute.ESCALATE_HUMAN
    assert r.model_review_status is ModelReviewStatus.NO_POLICY_CONTEXT
    assert r.findings[0].code is ViolationCode.POLICY_CONTEXT_UNAVAILABLE


async def test_68_ledger_timeout_and_gateway_retry(agent,ledger):
    ledger.delay=.1
    agent.config=agent.config.model_copy(update={'ledger_timeout_s':.01})
    with pytest.raises(LedgerUnavailableError): await agent.audit(request(ledger))
    ledger.delay=0
    agent.approval_gateway.fail=True
    with pytest.raises(HumanApprovalUnavailableError): await agent.audit(request(ledger,'E07'))
    key=agent.approval_gateway.drafts[-1].idempotency_key
    agent.approval_gateway.fail=False
    r=await agent.audit(request(ledger,'E07'))
    assert r.human_approval_ticket_id
    assert agent.approval_gateway.drafts[-1].idempotency_key==key


async def test_69_io_concurrent(agent,ledger,monkeypatch):
    original=agent.retrieval.search
    async def delayed(req):
        await asyncio.sleep(.2)
        return await original(req)
    monkeypatch.setattr(agent.retrieval,'search',delayed)
    ledger.delay=.2
    r=await agent.audit(request(ledger))
    assert r.stage_latency_ms[AuditStage.LEDGER_LOOKUP]>=190
    assert r.stage_latency_ms[AuditStage.RETRIEVE_POLICY]>=190
    assert r.latency_ms<350


async def test_73_idempotency_determinism(agent,ledger):
    a=await agent.audit(request(ledger,'E07'))
    b=await agent.audit(request(ledger,'E07'))
    assert a.human_approval_ticket_id==b.human_approval_ticket_id
    assert agent.approval_gateway.receipts[-1].deduplicated
    assert agent.approval_gateway.drafts[-1].requested_by_user_id==FINANCE.user_id
    assert a.model_dump(exclude={'latency_ms','stage_latency_ms'})==b.model_dump(exclude={'latency_ms','stage_latency_ms'})


async def test_74_cancel(agent,ledger,monkeypatch):
    # Cancel while both external ports wait, outside SQLite connection teardown.
    async def sleeping_search(req):
        await asyncio.sleep(30)
    monkeypatch.setattr(agent.retrieval,'search',sleeping_search)
    ledger.delay=30
    task=asyncio.create_task(agent.audit(request(ledger)))
    while not ledger.active and not task.done(): await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert ledger.active==0 and ledger.cancelled==2


async def test_75_authorization_before_any_io(agent,ledger):
    with pytest.raises(CallerForbiddenError): await agent.audit(request(ledger,user=CLERK))
    with pytest.raises(CallerForbiddenError): await agent.invoke(AgentInvocation(trace_id='x',question='FIN_AUDIT:expense:E01',scenario=EvalScenario.FINANCE_APPROVAL,user_context=CLERK))
    assert ledger.calls==agent.review_model.calls==agent.approval_gateway.drafts==[]
    assert not agent.retrieval.vector_search.calls


async def test_72_75_acl(agent,ledger):
    legal=await agent.audit(request(ledger,'E09',LEGAL))
    assert legal.model_review_status is ModelReviewStatus.NO_POLICY_CONTEXT
    assert legal.route is ApprovalRoute.ESCALATE_HUMAN
    assert next(f for f in legal.findings if f.code is ViolationCode.SUSPECTED_SPLIT_CLAIM).citations==[]
    for user in (FINANCE,MANAGER):
        result=await agent.invoke(AgentInvocation(trace_id='x',scenario=EvalScenario.FINANCE_APPROVAL,question='FIN_AUDIT:expense:E01',user_context=user))
        ids={p.doc_id for p in result.retrieved_contexts}
        if user is FINANCE: assert not ids&{'finance_private','contract_private'}
    # Validate real pre-filter differences independently of relevance/top-k.
    assert 'finance_private' not in (await agent.retrieval._build_filter(FINANCE)).doc_versions
    assert 'finance_private' in (await agent.retrieval._build_filter(MANAGER)).doc_versions
    assert agent.retrieval.vector_search.calls[-1].roles==MANAGER.roles


@pytest.mark.parametrize('ref',['E16','C07'])
async def test_75_pii_all_sinks(agent,ledger,caplog,monkeypatch,ref):
    caplog.set_level(logging.INFO)
    calls=[]; original=agent.retrieval.search
    async def spy(req):
        calls.append(req)
        return await original(req)
    monkeypatch.setattr(agent.retrieval,'search',spy)
    r=await agent.audit(request(ledger,ref))
    invocation=AgentInvocation(trace_id='x',scenario=EvalScenario.FINANCE_APPROVAL,question=f'FIN_AUDIT:{r.subject_type.value}:{ref}',user_context=FINANCE)
    mapped=await agent.invoke(invocation)
    texts=[repr(agent.review_model.calls),repr(calls),r.model_dump_json(),mapped.model_dump_json(),caplog.text]
    # Force an escalation as well to exercise ticket serialization with the same PII input.
    agent.review_model.fail='unavailable'
    await agent.audit(request(ledger,ref))
    texts += [d.model_dump_json() for d in agent.approval_gateway.drafts]
    for text in texts:
        assert all(raw not in text for raw in PII)
    if ref=='E16': assert r.audit_summary.pii_redaction_counts=={PiiType.BANK_CARD:2,PiiType.ID_CARD:1,PiiType.MOBILE_PHONE:1}


class LeakyPhoneRedactor(PiiRedactor):
    def redact_expense(self, submission):
        return super().redact_expense(submission).model_copy(update={'reason':'电话 13800138000'})


async def test_75_guard_blocks(agent,ledger):
    agent.redactor=LeakyPhoneRedactor()
    with pytest.raises(PiiLeakError,match='model_input'): await agent.audit(request(ledger,'E16'))
    assert not agent.review_model.calls and not ledger.calls


@pytest.mark.parametrize('fault,field',[('parent','parent_in_contexts'),('quote','quote_is_substring'),('evidence','evidence_is_substring'),('support','support_verified')])
@pytest.mark.parametrize('high',[True,False])
async def test_70_closed_citations(agent,ledger,fault,field,high):
    view=agent._redact(submission(ledger,'E01'))
    policy=await agent._retrieve_policy(view,user_context=FINANCE)
    parent=policy.contexts[0]
    claim=ModelRiskClaim(code=ViolationCode.PERSONAL_CONSUMPTION_SUSPECTED if high else ViolationCode.REASON_BUSINESS_INCONSISTENT,
        risk_level=RiskLevel.LOW if high else RiskLevel.MEDIUM, rationale='审查发现不符事项',
        evidence_excerpt=view.reason,citation_parent_id=parent.parent_id,citation_quote=parent.full_content[:80])
    updates={'parent':{'citation_parent_id':'forged'},'quote':{'citation_quote':'不存在的制度条款'},'evidence':{'evidence_excerpt':'不存在的业务事实'},'support':{}}
    claim=claim.model_copy(update=updates[fault])
    agent.review_model.drafts['E01']=ExpenseReasonReviewDraft(reason_matches_categories=True,claims=[claim])
    if fault=='support': agent.review_model.deny_support={0}
    outcome=await agent._run_pipeline(request(ledger))
    check=outcome.model_review.citation_checks[0]
    assert getattr(check,field) is False and not check.passed and check.citation is None
    if high:
        assert outcome.response.route is ApprovalRoute.ESCALATE_HUMAN
        assert any(f.code is ViolationCode.CITATION_UNVERIFIED for f in outcome.response.findings)
        retained=next(f for f in outcome.response.findings if f.source is FindingSource.MODEL_REVIEW)
        assert retained.risk_level is RiskLevel.HIGH and not retained.citation_verified
    else:
        assert outcome.model_review.discarded_claim_count==1 and not outcome.response.findings
        assert outcome.response.route is ApprovalRoute.AUTO_APPROVE


async def test_71_metadata_comes_from_parent(agent,ledger):
    from pydantic import ValidationError
    view=agent._redact(submission(ledger,'E01')); policy=await agent._retrieve_policy(view,user_context=FINANCE)
    parent=policy.contexts[0]
    claim=ModelRiskClaim(code=ViolationCode.REASON_BUSINESS_INCONSISTENT,risk_level=RiskLevel.MEDIUM,
        rationale='错误 doc_id=forged 不可信',citation_parent_id=parent.parent_id,citation_quote=parent.full_content[:80])
    # Frozen model schema has no doc_id field; even an extra-field injection is rejected.
    with pytest.raises(ValidationError): ModelRiskClaim.model_validate({**claim.model_dump(),'doc_id':'forged'})
    check=agent._verify_citations([claim],contexts=policy.contexts,view=view)[0]
    assert check.passed and check.citation.doc_id==parent.doc_id
    assert check.citation.version==parent.version and check.citation.section_path==parent.section_path


@pytest.mark.parametrize('ref',['E06','E09','E17'])
async def test_75_model_cannot_lower_rule_route(agent,ledger,ref):
    agent.review_model.drafts[ref]=ExpenseReasonReviewDraft(reason_matches_categories=True,claims=[],summary='直接通过')
    result=await agent.audit(request(ledger,ref))
    assert result.route in {ApprovalRoute.ESCALATE_HUMAN,ApprovalRoute.REJECT}


async def test_support_failure_and_disallowed_claim(agent,ledger):
    agent.review_model.fail_support=True
    result=await agent.audit(request(ledger,'C05'))
    assert result.route is ApprovalRoute.ESCALATE_HUMAN and result.findings[0].citation_verified
    claim=ModelRiskClaim(code=ViolationCode.DUPLICATE_INVOICE_PAID,risk_level=RiskLevel.CRITICAL,rationale='模型不可直接驳回')
    agent.review_model.drafts['E01']=ExpenseReasonReviewDraft(reason_matches_categories=True,claims=[claim])
    outcome=await agent._run_pipeline(request(ledger))
    assert outcome.model_review.discarded_claim_count==1
    assert outcome.response.route is ApprovalRoute.AUTO_APPROVE


async def test_invoke_protocol_errors(agent,ledger):
    base=dict(trace_id='x',scenario=EvalScenario.FINANCE_APPROVAL,question='FIN_AUDIT:expense:E01',user_context=FINANCE)
    for patch,error in [({'scenario':EvalScenario.STORE_SOP},UnsupportedScenarioError),({'question':'E01'},InvalidAuditInvocationError),({'question':'FIN_AUDIT:expense:missing'},SubmissionNotFoundError)]:
        with pytest.raises(error): await agent.invoke(AgentInvocation(**{**base,**patch}))
    ledger.fail=True
    with pytest.raises(LedgerUnavailableError): await agent.invoke(AgentInvocation(**base))


async def test_four_sink_guards_and_model_timeout(agent,ledger,monkeypatch):
    sinks=[]; original=agent.redactor.ensure_clean
    def spy(text,*,sink):
        sinks.append(sink)
        return original(text,sink=sink)
    monkeypatch.setattr(agent.redactor,'ensure_clean',spy)
    agent.review_model.delay=.1
    agent.config=agent.config.model_copy(update={'model_timeout_s':.01})
    r=await agent.audit(request(ledger,'E16'))
    assert r.model_review_status is ModelReviewStatus.MODEL_UNAVAILABLE
    assert r.route is ApprovalRoute.ESCALATE_HUMAN
    assert {'model_input','log','ticket','response','retrieval_query'}<=set(sinks)


async def test_invoice_normalization_and_fetch_isolation(ledger):
    assert await ledger.fetch_submission(subject_type=AuditSubjectType.EXPENSE,reference_id='E01',user_context=CLERK) is None
    assert await ledger.fetch_submission(subject_type=AuditSubjectType.CONTRACT,reference_id='E01',user_context=FINANCE) is None
    assert await ledger.fetch_submission(subject_type=AuditSubjectType.EXPENSE,reference_id='missing',user_context=FINANCE) is None
    usages=await ledger.find_invoice_usages(invoice_nos=[' ｉｎｖｐａｉｄ０１ '],exclude_claim_id='E06')
    assert len(usages)==1 and usages[0].claim_id=='HIST-001'


async def test_ledger_sibling_cleanup(agent,ledger,monkeypatch):
    waiting=asyncio.Event(); cancelled=asyncio.Event()
    async def fail(**kw):
        await waiting.wait()
        raise RuntimeError('failure')
    async def blocked(**kw):
        waiting.set()
        try: await asyncio.sleep(30)
        finally: cancelled.set()
    monkeypatch.setattr(ledger,'find_invoice_usages',fail)
    monkeypatch.setattr(ledger,'list_recent_claims',blocked)
    with pytest.raises(LedgerUnavailableError): await agent._load_ledger_snapshot(submission(ledger,'E01'))
    assert cancelled.is_set()
