import asyncio
import pytest
from agent_matrix.store_sop.models import IntentSlotExtraction, ExtractedSlots
from eval_platform.models import AgentInvocation, FailureCategory, GateViolationCode
from eval_platform.report_generator import MarkdownReportGenerator
from eval_platform.runner import load_eval_suite
from tests.agent_matrix.store_sop.fakes import ScriptedChatModel
from tests.agent_matrix.store_sop.run_e2e import SUITE, compare, make_agent


@pytest.fixture
async def samples():
    return await load_eval_suite(SUITE)


async def test_suite_coverage(samples):
    assert len(samples)>=10
    assert all(s.scenario.value=='store_sop' for s in samples)
    assert sum('drink' in s.tags for s in samples)>=3
    equipment=[s for s in samples if 'equipment' in s.tags]
    assert len(equipment)>=3 and all('page_2' in s.tags for s in equipment)
    assert all(any(w in s.question for w in ['不进水','冰块偏薄','漏水','封口不牢','不加热']) for s in equipment)
    assert sum(s.is_acl_redline for s in samples)>=3
    assert any('numeric_hallucination_trap' in s.tags for s in samples)


async def test_e2e_gate_report_and_negative_control(sop_service,samples,tmp_path):
    # A question-indexed script demonstrates deterministic structured output while
    # answer generation still sees only the source passages, never evaluation labels.
    model=ScriptedChatModel(by_question={
        '鲜柠檬茶 500ml 柠檬片多少克':{
            IntentSlotExtraction:IntentSlotExtraction(intent='drink_sop',confidence=1,slots=ExtractedSlots(drink_name='鲜柠檬茶'))}})
    baseline,candidate,diff,leaked,leak_diff=await compare(sop_service,samples,model)
    assert baseline.summary.acl_redline_passed
    assert baseline.summary.retrieval_hit_rate>=.8
    assert baseline.summary.error_rate==0
    assert all(result.token_cost>0 for result in baseline.results)
    assert candidate.summary.acl_redline_passed
    assert all(r.failure_category!=FailureCategory.ACL_LEAK for r in candidate.results)
    assert GateViolationCode.METRIC_REGRESSION in [v.code for v in diff.violations]
    assert not diff.gate_passed
    assert leak_diff.violations[0].code==GateViolationCode.ACL_REDLINE_BREACHED
    assert any(r.acl_leaked and 'fin_approval_matrix' in r.leaked_doc_ids for r in leaked.results)
    renderer=MarkdownReportGenerator()
    text=renderer.render(candidate,diff)
    for violation in diff.violations:
        assert violation.code.value in text
    assert 'acl_redline_breached' in renderer.render(leaked,leak_diff)
    output=tmp_path/'store_sop_e2e_report.md'
    await renderer.write(output,text)
    assert await asyncio.to_thread(output.read_text,encoding='utf-8')==text


async def test_redline_responses_and_eval_isolation(sop_service,samples):
    agent=make_agent(sop_service)
    responses=await asyncio.gather(*(agent.invoke(AgentInvocation(trace_id=s.sample_id,scenario=s.scenario,
        question=s.question,user_context=s.user_context)) for s in samples if s.is_acl_redline))
    assert all(r.refused and r.retrieved_contexts==[] and r.cited_doc_ids==[] for r in responses)
    assert agent.session_store.load_calls==agent.session_store.save_calls==0
