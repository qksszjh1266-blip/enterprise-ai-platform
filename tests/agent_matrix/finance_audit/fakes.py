"""Deterministic offline port adapters, with observable fault injection."""
import asyncio
import hashlib
import json
from pathlib import Path
from agent_matrix.finance_audit.models import (
    AuditSubjectType, CitationSupportDraft, CitationSupportVerdict, ContractAuditSubmission,
    ContractClauseReviewDraft, ContractTermExtraction, ExpenseAuditSubmission, ExpenseReasonReviewDraft,
    FinanceModelResult, HistoricalClaimRecord, HistoricalContractRecord, HumanApprovalTicketReceipt,
    InvoiceUsageRecord, ModelRiskClaim, RiskLevel, ViolationCode,
)
from agent_matrix.finance_audit.errors import FinanceModelUnavailableError, FinanceStructuredOutputError
from agent_matrix.finance_audit.rule_engine import _invoice_key
from eval_platform.models import TokenUsage

DATA = Path(__file__).parent/'data'


class ScriptedFinanceModel:
    def __init__(self, drafts=None, *, fail=None, delay=0, token_usage=None):
        self.drafts = drafts or {}
        self.fail, self.delay = fail, delay
        self.token_usage = token_usage or TokenUsage(prompt_tokens=10,completion_tokens=5)
        self.calls=[]
        self.deny_support=set()
        self.fail_support=False

    async def _call(self, method, **arguments):
        self.calls.append((method, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail=='timeout':
            raise TimeoutError()
        if self.fail=='unavailable':
            raise FinanceModelUnavailableError('offline fault')
        if self.fail=='invalid':
            raise FinanceStructuredOutputError('offline fault')

    def _result(self, output):
        return FinanceModelResult(output=output,token_usage=self.token_usage,model_name='scripted-finance-v1')

    async def review_expense_reason(self, *, view, policy_contexts):
        await self._call('expense',view=view,policy_contexts=policy_contexts)
        return self._result(self.drafts.get(view.claim_id, ExpenseReasonReviewDraft(reason_matches_categories=True)))

    async def review_contract_clauses(self, *, view, policy_contexts):
        await self._call('contract',view=view,policy_contexts=policy_contexts)
        if view.contract_id in self.drafts:
            return self._result(self.drafts[view.contract_id])
        claims=[]
        if view.contract_id=='C05':
            parent=next(p for p in policy_contexts if p.doc_id=='contract_breach')
            claims=[ModelRiskClaim(code=ViolationCode.UNLIMITED_LIABILITY,risk_level=RiskLevel.HIGH,
                rationale='条款约定我方承担无上限责任', evidence_excerpt='不设上限',
                citation_parent_id=parent.parent_id,citation_quote='不得约定我方无上限责任')]
        terms=ContractTermExtraction(stated_payment_term_days=45 if view.contract_id=='C08' else None)
        return self._result(ContractClauseReviewDraft(claims=claims,extracted_terms=terms))

    async def verify_citation_support(self, *, claims, policy_contexts):
        await self._call('support',claims=claims,policy_contexts=policy_contexts)
        if self.fail_support:
            raise FinanceModelUnavailableError('support offline')
        return self._result(CitationSupportDraft(verdicts=[CitationSupportVerdict(claim_index=i,supported=i not in self.deny_support) for i in range(len(claims))]))


class SeededClaimLedger:
    def __init__(self, data, *, fail=False, delay=0):
        self.submissions={}
        for row in data['submissions']:
            model=ExpenseAuditSubmission if row['subject_type']=='expense' else ContractAuditSubmission
            parsed=model.model_validate(row)
            ref=parsed.claim_id if isinstance(parsed,ExpenseAuditSubmission) else parsed.contract_id
            self.submissions[(AuditSubjectType(parsed.subject_type),ref)]=parsed
        self.invoice_usages=[InvoiceUsageRecord.model_validate(r) for r in data['invoice_usages']]
        self.claims=[HistoricalClaimRecord.model_validate(r) for r in data['claims']]
        self.contracts=[HistoricalContractRecord.model_validate(r) for r in data['contracts']]
        self.fail,self.delay=fail,delay
        self.calls=[]
        self.active=0
        self.cancelled=0

    @classmethod
    async def from_file(cls, path=DATA/'finance_audit_cases.json', **kwargs):
        data=json.loads(await asyncio.to_thread(path.read_text,encoding='utf-8'))
        return cls(data,**kwargs)

    async def _before(self, name, **kwargs):
        self.calls.append((name,kwargs))
        self.active+=1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError('ledger unavailable')
        except asyncio.CancelledError:
            self.cancelled+=1
            raise
        finally:
            self.active-=1

    async def find_invoice_usages(self, *, invoice_nos, exclude_claim_id):
        await self._before('invoices',invoice_nos=invoice_nos,exclude_claim_id=exclude_claim_id)
        keys={_invoice_key(n) for n in invoice_nos}
        return [r for r in self.invoice_usages if _invoice_key(r.invoice_no) in keys and r.claim_id!=exclude_claim_id]

    async def list_recent_claims(self, *, applicant_user_id, store_code, categories, window_start, window_end, exclude_claim_id):
        await self._before('claims',window_start=window_start,window_end=window_end)
        return [r for r in self.claims if r.claim_id!=exclude_claim_id and r.applicant_user_id==applicant_user_id
                and r.store_code==store_code and r.category in categories and window_start<=r.submitted_at<=window_end]

    async def list_recent_contracts(self, *, counterparty_credit_code, window_start, window_end, exclude_contract_id):
        await self._before('contracts',window_start=window_start,window_end=window_end)
        return [r for r in self.contracts if r.contract_id!=exclude_contract_id and r.counterparty_credit_code==counterparty_credit_code
                and window_start<=r.submitted_at<=window_end]

    async def fetch_submission(self, *, subject_type, reference_id, user_context):
        await self._before('fetch',subject_type=subject_type,reference_id=reference_id,user_context=user_context)
        if user_context.user_id=='store_clerk_01' or user_context.dept_id!='finance' or not set(user_context.roles)&{'finance','finance_manager','legal'}:
            return None
        return self.submissions.get((subject_type,reference_id))


class RecordingApprovalGateway:
    def __init__(self, *, fail=False):
        self.fail=fail
        self.drafts=[]
        self.receipts=[]
        self.tickets={}

    async def create_review_ticket(self, draft):
        self.drafts.append(draft)
        if self.fail:
            raise RuntimeError('approval unavailable')
        key=draft.idempotency_key
        if key in self.tickets:
            receipt=self.tickets[key].model_copy(update={'deduplicated':True})
        else:
            receipt=HumanApprovalTicketReceipt(ticket_id='FA-'+hashlib.sha256(key.encode()).hexdigest()[:16],
                queue=draft.queue,created_at=draft.created_at)
            self.tickets[key]=receipt
        self.receipts.append(receipt)
        return receipt
