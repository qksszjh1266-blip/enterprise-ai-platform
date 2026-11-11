from datetime import datetime, timezone
import pytest
from agent_matrix.store_sop import prompts as p
from agent_matrix.store_sop.models import *
from data_platform.retrieval_engine.models import DegradedReason, UserContext
from tests.eval_platform.conftest import chunk


@pytest.mark.parametrize('intent',[None,*StoreIntent])
@pytest.mark.parametrize('degraded',[False,True])
def test_composition(intent,degraded):
    parts=[p.SYSTEM_PERSONA,p.GROUNDING_RULES]
    if intent==StoreIntent.DRINK_SOP: parts.append(p.DRINK_SOP_RULES)
    if intent==StoreIntent.EQUIPMENT_FAULT: parts.extend([p.EQUIPMENT_CLARIFY_POLICY,p.SAFETY_FIRST_RULES])
    parts.append(p.CROSS_DEPT_REFUSAL_POLICY)
    if degraded: parts.append(p.DEGRADED_RISK_RULES)
    assert p.compose_system_prompt(intent=intent,degraded=degraded)=='\n\n'.join(parts)


def test_messages_escape_and_identity():
    now=datetime.now(timezone.utc)
    user=UserContext(user_id='SECRET_UID',dept_id='SECRET_DEPT',roles=['SECRET_ROLE'])
    state=StoreSessionState(session_id='s',user_context=user,created_at=now,updated_at=now)
    question='{x} SECRET_UID SECRET_DEPT SECRET_ROLE'
    messages=[*p.build_intent_slot_messages(state=state,user_message=question),
        *p.build_next_action_messages(state=state,user_message=question,observations=[]),
        *p.build_grounded_answer_messages(state=state,question=question,contexts=[chunk()],degraded_reasons=[]),
        *p.build_summary_messages(state=state)]
    text='\n'.join(m.content for m in messages)
    assert '{{x}}' in text
    assert not any(value in text for value in [user.user_id,user.dept_id,*user.roles])
    with pytest.raises(ValueError):
        p.build_grounded_answer_messages(state=state,question='x',contexts=[],degraded_reasons=[])


def test_context_numbering():
    contexts=[chunk('secret_doc','secret_parent').model_copy(update={'section_path':'章节','page_number':2}),chunk('doc2','parent2')]
    text=p.render_context_block(contexts)
    assert '[来源1] 章节：章节（第 2 页）' in text
    assert '[来源2]' in text and '第 None 页' not in text
    assert all(s not in text for s in ['secret_doc','secret_parent','doc2','parent2'])


def test_clarification_and_refusal():
    text=p.render_clarification_question(missing_slots=list(SlotName)[:3],slots=ExtractedSlots(),safety_hazard=True)
    assert text.startswith(p.SAFETY_CLARIFICATION_PREFIX)
    assert '\n2.' in text and '\n3.' not in text
    with pytest.raises(ValueError):
        p.render_clarification_question(missing_slots=[],slots=ExtractedSlots(),safety_hazard=False)
    assert not any(k in p.render_refusal(RefusalReason.CROSS_DEPT) for k in ['审批矩阵','fin_','财务部','区域经理'])


def test_degraded_labels():
    assert p.render_degraded_notice([DegradedReason.KEYWORD_TIMEOUT]*2).count('关键词检索超时')==1
    assert '部分服务不可用' in p.render_degraded_notice([])
