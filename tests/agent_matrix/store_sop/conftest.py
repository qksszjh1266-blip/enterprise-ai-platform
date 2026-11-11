import asyncio
import pytest
from agent_matrix.store_sop.agent import StoreSopAgent
from data_platform.retrieval_engine.models import UserContext
from data_platform.storage_sync.models import DocumentIngestRequest, SyncOutcome
from tests.eval_platform.corpus import ROOT, publish_corpus
from tests.agent_matrix.store_sop.fakes import ScriptedChatModel, InMemorySessionStore, FakeTicketPort


async def publish_store_corpus(manager):
    await publish_corpus(manager)
    content = await asyncio.to_thread((ROOT / 'mock_docs/store_sop_scanner_training.md').read_text, encoding='utf-8')
    result = await manager.sync_document(DocumentIngestRequest(doc_id='sop_scanner', title='扫码枪培训演示',
        source_system='training', content=content, dept_id='store_ops', allowed_roles=['clerk','store_manager']))
    assert result.outcome == SyncOutcome.PUBLISHED
    await manager.aclose()


@pytest.fixture
async def sop_service(manager, retrieval):
    await publish_store_corpus(manager)
    return retrieval


@pytest.fixture
def user():
    return UserContext(user_id='trusted-user-001', dept_id='store_ops', roles=['clerk'])


@pytest.fixture
def model():
    return ScriptedChatModel()


@pytest.fixture
def sessions():
    return InMemorySessionStore()


@pytest.fixture
def tickets():
    return FakeTicketPort()


@pytest.fixture
async def agent(sop_service, model, sessions, tickets):
    return StoreSopAgent(chat_model=model, retrieval=sop_service, session_store=sessions, ticket_port=tickets)
