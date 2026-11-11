from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from agent_matrix.store_sop import errors
from agent_matrix.store_sop.models import *
from agent_matrix.store_sop.ports import ChatModelPort, SessionStorePort, TicketEscalationPort
from eval_platform.ports import AgentSystemPort


@pytest.mark.parametrize('change',[
    {'needs_clarification':True},
    {'stop_reason':'clarification_requested','needs_clarification':True},
    {'stop_reason':'escalated'}, {'stop_reason':'answered'}, {'refused':True}, {'token_cost':1},
    {'clarification_slots':['fault_symptom']},
])
def test_response_invariants(change):
    with pytest.raises(ValidationError):
        StoreAgentTurnResponse.model_validate(dict(session_id='s',turn_index=1,answer='x',
            stop_reason='max_steps_reached',steps_used=1,latency_ms=0) | change)


def ticket_values():
    return dict(idempotency_key='s:e:f', session_id='s',reporter_user_id='u',dept_id='d',
        equipment_model='制冰机',fault_symptom='焦味',severity='p1_safety',summary='故障',created_at=datetime.now(timezone.utc))


def test_ticket_invariants_and_clarification_exclusion():
    with pytest.raises(ValidationError):
        RepairTicketDraft(**(ticket_values() | {'safety_hazard':True,'severity':'p2_business_blocking'}))
    draft = RepairTicketDraft(**ticket_values())
    with pytest.raises(ValidationError):
        StoreAgentTurnResponse(session_id='s',turn_index=1,answer='x',stop_reason='clarification_requested',
            needs_clarification=True,clarification_slots=['fault_symptom'],escalation_ticket=draft,steps_used=1,latency_ms=0)


def test_model_identity_injection():
    with pytest.raises(ValidationError):
        IntentSlotExtraction.model_validate({'intent':'drink_sop','confidence':1,'user_context':{'roles':['finance']}})


async def test_protocols(agent, model, sessions, tickets):
    assert isinstance(agent, AgentSystemPort)
    assert isinstance(model, ChatModelPort)
    assert isinstance(sessions, SessionStorePort)
    assert isinstance(tickets, TicketEscalationPort)


@pytest.mark.parametrize('error_type',[errors.StoreAgentError,*errors.StoreAgentError.__subclasses__()])
def test_errors(error_type):
    exc=error_type('失败',session_id='abc')
    assert exc.to_hint()==f'[{exc.code}] session_id=abc | 失败 | 建议: {exc.suggestion}'
    assert 'session_id=-' in error_type('失败').to_hint()
