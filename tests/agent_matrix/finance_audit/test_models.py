"""Acceptance #53–58: exercise the frozen contract, without editing it."""
from decimal import Decimal
from datetime import timedelta
import pytest
from pydantic import TypeAdapter, ValidationError
from agent_matrix.finance_audit.models import *
from agent_matrix.finance_audit.ports import FinanceReviewModelPort, ClaimHistoryLedgerPort, HumanApprovalGatewayPort
from agent_matrix.finance_audit.eval_judge import FinanceAuditJudge
from agent_matrix.finance_audit.rule_engine import DeterministicFinanceRuleEngine
from eval_platform.ports import AgentSystemPort, LLMJudgePort
from tests.agent_matrix.finance_audit.conftest import FINANCE, request, submission
from tests.agent_matrix.finance_audit.test_rule_engine import snapshot

@pytest.mark.parametrize('kind',[Money,NonNegativeMoney,PositiveMoney,TaxRate,Quantity])
def test_53_decimal_types(kind):
    adapter=TypeAdapter(kind)
    with pytest.raises(ValidationError): adapter.validate_python(.1)
    assert adapter.dump_json(adapter.validate_python('0.10'))==b'"0.10"'
    # TaxRate and Quantity deliberately permit four decimals in the frozen schema.
    invalid='128.505' if kind in (Money,NonNegativeMoney,PositiveMoney) else '0.12345'
    with pytest.raises(ValidationError): adapter.validate_python(invalid)


@pytest.mark.parametrize('mutation',['lines','invoice','datetime'])
async def test_54_structure(ledger,mutation):
    data=submission(ledger,'E01').model_dump()
    if mutation=='lines': data['lines'][0]['line_no']=2
    if mutation=='invoice': data['lines'][0]['invoice_no']='NOTFOUND1'
    if mutation=='datetime': data['submitted_at']=data['submitted_at'].replace(tzinfo=None)
    with pytest.raises(ValidationError): ExpenseAuditSubmission.model_validate(data)
    data=submission(ledger,'E01').model_dump(); data['declared_total']='999.99'
    assert ExpenseAuditSubmission.model_validate(data).declared_total==Decimal('999.99')


def base_finding():
    return dict(finding_id='f1',code=ViolationCode.DUPLICATE_INVOICE_PAID,risk_level=RiskLevel.CRITICAL,
        source=FindingSource.RULE_ENGINE,min_route=ApprovalRoute.REJECT,is_redline=True,title='重复支付',detail='重复支付',rule_id='FIN-R07')


@pytest.mark.parametrize('patch',[{'source':FindingSource.MODEL_REVIEW},{'risk_level':RiskLevel.LOW},
    {'min_route':ApprovalRoute.AUTO_APPROVE},{'is_redline':False},{'rule_id':None},{'citation_verified':True}])
def test_55_finding_policy(patch):
    assert ComplianceFinding(**base_finding())
    with pytest.raises(ValidationError): ComplianceFinding(**{**base_finding(),**patch})


def test_55_supplement_and_high_policy():
    data={**base_finding(),'code':ViolationCode.MISSING_ATTACHMENT,'is_redline':False,'min_route':ApprovalRoute.NEEDS_SUPPLEMENT,'risk_level':RiskLevel.LOW}
    with pytest.raises(ValidationError): ComplianceFinding(**data)
    assert ComplianceFinding(**data,supplement_request='请补充发票')
    data.update(risk_level=RiskLevel.HIGH,supplement_request='请补充发票')
    with pytest.raises(ValidationError): ComplianceFinding(**data)


@pytest.mark.parametrize('ref',['E01','E06','E07','E12','E17'])
async def test_56_response_invariants(agent,ledger,ref):
    response=await agent.audit(request(ledger,ref))
    data=response.model_dump()
    assert FinanceAuditResponse.model_validate(data)
    for patch in [{'token_cost':response.token_cost+1}, {'max_risk_level':RiskLevel.HIGH if response.max_risk_level is not RiskLevel.HIGH else RiskLevel.LOW}]:
        with pytest.raises(ValidationError): FinanceAuditResponse.model_validate({**data,**patch})
    if ref=='E01':
        for patch in [{'route':ApprovalRoute.REJECT}, {'citation_check_passed':False}, {'model_review_status':ModelReviewStatus.MODEL_UNAVAILABLE}, {'human_approval_ticket_id':'bad'}]:
            if 'route' in patch:
                patch['audit_summary']={**data['audit_summary'],'route':patch['route']}
            with pytest.raises(ValidationError): FinanceAuditResponse.model_validate({**data,**patch})
    if ref in {'E06','E07','E17'}:
        for route in (ApprovalRoute.AUTO_APPROVE,ApprovalRoute.NEEDS_SUPPLEMENT):
            patch={'route':route,'human_approval_ticket_id':None,'audit_summary':{**data['audit_summary'],'route':route}}
            with pytest.raises(ValidationError): FinanceAuditResponse.model_validate({**data,**patch})
    if ref=='E07':
        with pytest.raises(ValidationError): FinanceAuditResponse.model_validate({**data,'human_approval_ticket_id':None})
    if ref=='E12':
        with pytest.raises(ValidationError): FinanceAuditResponse.model_validate({**data,'supplement_requests':[]})


async def test_57_consistency_models(ledger):
    s=submission(ledger,'E01'); ledger_snapshot=snapshot(s)
    assert LedgerSnapshot.model_validate(ledger_snapshot.model_dump())
    with pytest.raises(ValidationError): LedgerSnapshot.model_validate({**ledger_snapshot.model_dump(),'window_start':s.submitted_at+timedelta(days=1)})
    with pytest.raises(ValidationError): LedgerSnapshot.model_validate({**ledger_snapshot.model_dump(),'recent_contracts':[ledger.contracts[0]]})
    contract_snapshot=snapshot(submission(ledger,'C01'))
    with pytest.raises(ValidationError): LedgerSnapshot.model_validate({**contract_snapshot.model_dump(),'invoice_usages':[ledger.invoice_usages[0]]})
    r=DeterministicFinanceRuleEngine().evaluate_expense(s,ledger=ledger_snapshot,caller=FINANCE).reconciliation
    for patch in [{'total_deviation':Decimal('1')},{'has_duplicate_invoice':True},{'split_suspected':True},{'total_within_tolerance':False},{'tax_check_passed':False},
                  {'subject_type':AuditSubjectType.CONTRACT}]:
        with pytest.raises(ValidationError): AmountReconciliationResult.model_validate({**r.model_dump(),**patch})
    assert DuplicateInvoiceFlag(invoice_no='X',code=ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION)
    for patch in [{'code':ViolationCode.LINE_AMOUNT_MISMATCH},{'conflicting_claim_id':'X'},{'code':ViolationCode.DUPLICATE_INVOICE_PAID}]:
        with pytest.raises(ValidationError): DuplicateInvoiceFlag.model_validate({'invoice_no':'X','code':ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION,**patch})
    check=dict(claim_index=0,code=ViolationCode.UNLIMITED_LIABILITY,parent_in_contexts=False,quote_is_substring=False,evidence_is_substring=True,passed=False)
    assert CitationCheckResult(**check)
    with pytest.raises(ValidationError): CitationCheckResult(**{**check,'passed':True})
    assert RouteDecision(route=ApprovalRoute.AUTO_APPROVE,approver_tier=ApproverTier.STORE_MANAGER)
    with pytest.raises(ValidationError): RouteDecision(route=ApprovalRoute.ESCALATE_HUMAN,approver_tier=ApproverTier.STORE_MANAGER)
    with pytest.raises(ValidationError): RouteDecision(route=ApprovalRoute.AUTO_APPROVE,approver_tier=ApproverTier.STORE_MANAGER,reviewer_queue=ReviewerQueue.LEGAL_COUNSEL)


@pytest.mark.parametrize('lower',['499','501'])
def test_57_approval_bands(lower):
    bands=[b.model_dump() for b in DEFAULT_APPROVAL_BANDS]; bands[1]['lower_exclusive']=lower
    with pytest.raises(ValidationError): FinanceRuleConfig(approval_bands=bands)
    assert FinanceRuleConfig()


async def test_58_protocols(agent):
    assert isinstance(agent,AgentSystemPort)
    assert isinstance(agent.review_model,FinanceReviewModelPort)
    assert isinstance(agent.ledger,ClaimHistoryLedgerPort)
    assert isinstance(agent.approval_gateway,HumanApprovalGatewayPort)
    assert isinstance(FinanceAuditJudge(),LLMJudgePort)


def test_75_model_identity_and_route_injection():
    with pytest.raises(ValidationError): ExpenseReasonReviewDraft.model_validate({'reason_matches_categories':True,'route':'AUTO_APPROVE'})
    for schema,base in [(ExpenseReasonReviewDraft,{'reason_matches_categories':True}),(ContractClauseReviewDraft,{'extracted_terms':{}}),(CitationSupportDraft,{})]:
        with pytest.raises(ValidationError): schema.model_validate({**base,'user_context':{'roles':['finance_manager']}})
