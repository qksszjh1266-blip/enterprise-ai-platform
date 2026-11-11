import asyncio
from pathlib import Path
import pytest
from agent_matrix.finance_audit.agent import FinanceAuditAgent
from agent_matrix.finance_audit.models import AuditSubjectType, FinanceAuditRequest
from data_platform.retrieval_engine.models import UserContext, RetrievalConfig
from data_platform.storage_sync.models import DocumentIngestRequest, SyncOutcome
from tests.eval_platform.corpus import publish_corpus
from tests.agent_matrix.finance_audit.fakes import ScriptedFinanceModel, SeededClaimLedger, RecordingApprovalGateway

FINANCE=UserContext(user_id='fin_clerk_01',dept_id='finance',roles=['finance'])
MANAGER=UserContext(user_id='fin_mgr_01',dept_id='finance',roles=['finance_manager'])
LEGAL=UserContext(user_id='legal_01',dept_id='finance',roles=['legal'])
CLERK=UserContext(user_id='store_clerk_01',dept_id='store_ops',roles=['store_clerk'])


@pytest.fixture
async def ledger():
    return await SeededClaimLedger.from_file()


@pytest.fixture
async def finance_retrieval(manager,retrieval):
    await publish_corpus(manager,extended=True)
    source=Path(__file__).parent/'data/supplier_contract_policy.md'
    text=await asyncio.to_thread(source.read_text,encoding='utf-8')
    for doc_id,section in zip(('contract_payment','contract_breach','contract_approval','contract_private'),text.split('\n## ')[1:]):
        result=await manager.sync_document(DocumentIngestRequest(doc_id=doc_id,title=doc_id,source_system='oa',
            content='## '+section,dept_id='finance',allowed_roles=['finance_manager'] if doc_id=='contract_private' else ['finance','finance_manager','legal']))
        assert result.outcome is SyncOutcome.PUBLISHED
    await manager.aclose()
    retrieval.config=RetrievalConfig(branch_timeout_s=1,embedding_timeout_s=1,rerank_timeout_s=1)
    return retrieval


@pytest.fixture
async def agent(ledger,finance_retrieval):
    return FinanceAuditAgent(review_model=ScriptedFinanceModel(),ledger=ledger,
        approval_gateway=RecordingApprovalGateway(),retrieval=finance_retrieval)


def submission(ledger,ref):
    return ledger.submissions[(AuditSubjectType.EXPENSE if ref.startswith('E') else AuditSubjectType.CONTRACT,ref)]


def request(ledger,ref='E01',user=FINANCE):
    return FinanceAuditRequest(submission=submission(ledger,ref),user_context=user,trace_id='finance-test')
