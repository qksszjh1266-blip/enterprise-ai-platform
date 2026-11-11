import asyncio
from datetime import datetime, timezone
import pytest
from agent_matrix.store_sop.agent import StoreSopAgent
from agent_matrix.store_sop import prompts
from agent_matrix.store_sop.models import *
from agent_matrix.store_sop.errors import *
from eval_platform.models import AgentInvocation
from tests.eval_platform.conftest import chunk


def request(user,message='鲜柠檬茶 500ml 柠檬片多少克',session=None):
    return StoreAgentTurnRequest(user_context=user,message=message,session_id=session)


def extraction(intent='drink_sop',**slots):
    return IntentSlotExtraction(intent=intent,confidence=1,slots=ExtractedSlots(**slots))


async def test_drink_and_trace(agent,user,model,sop_service):
    response=await agent.handle_turn(request(user))
    assert response.stop_reason==StopReason.ANSWERED, response
    assert '40g' in response.answer
    assert response.citations[0].doc_id=='sop_drink'
    assert '标准配方与操作' in response.citations[0].section_path
    assert response.token_cost==sum(t.token_usage.total_tokens for t in response.tool_traces)>0
    assert [t.step_index for t in response.tool_traces]==list(range(1,response.steps_used+1))
    assert sum(t.tool_name.startswith('chat_model') for t in response.tool_traces)==len(model.calls)
    assert sum(t.step==LoopStep.RETRIEVE for t in response.tool_traces)==1
    state=await agent.session_store.load(response.session_id)
    assert state.turn_count==1 and state.state_version==1


@pytest.mark.parametrize('draft',[
    GroundedAnswerDraft(answer='柠檬片 45g [来源1]',cited_context_indices=[1]),
    GroundedAnswerDraft(answer='40g [来源9]',cited_context_indices=[9]),
    GroundedAnswerDraft(answer='40g [来源2]',cited_context_indices=[1]),
    GroundedAnswerDraft(answer='40g',cited_context_indices=[]),
    GroundedAnswerDraft(answer='40g [来源1]',cited_context_indices=[1],insufficient_evidence=True),
    GroundedAnswerDraft(answer='40g',cited_context_indices=[1]),
])
async def test_fail_closed(agent,user,model,draft):
    model.scripts[GroundedAnswerDraft].append(draft)
    outcome=await agent._run_loop(agent._new_session(session_id='s',user_context=user),message=request(user).message,trace_id=None)
    assert outcome.response.stop_reason==StopReason.REFUSED
    assert outcome.response.citations==[] and outcome.generation_contexts==[]
    assert outcome.response.tool_traces[-1].error_code=='CITATION_UNVERIFIED'


async def test_multiturn_and_escalation(agent,user,tickets,sop_service):
    one=await agent.handle_turn(request(user,'制冰机坏了'))
    assert one.stop_reason==StopReason.CLARIFICATION_REQUESTED
    assert one.clarification_slots==[SlotName.FAULT_SYMPTOM]
    assert not sop_service.vector_search.calls
    two=await agent.handle_turn(request(user,'不进水',one.session_id))
    assert two.stop_reason==StopReason.ANSWERED and '确认进水阀开启' in two.answer
    assert two.citations[0].doc_id=='sop_equipment'
    assert (await agent.session_store.load(one.session_id)).turn_count==2
    three=await agent.handle_turn(request(user,'都检查过了还是不进水，设备 ICE-008',one.session_id))
    assert three.stop_reason==StopReason.ESCALATED
    assert three.escalation_ticket.equipment_id=='ICE-008'
    assert three.ticket_receipt.status==TicketStatus.CREATED
    four=await agent.handle_turn(request(user,'都检查过了还是不进水，设备 ICE-008',one.session_id))
    assert four.ticket_receipt.ticket_id==three.ticket_receipt.ticket_id
    assert len(tickets.submitted)==1
    assert any(t.step==LoopStep.ESCALATE for t in three.tool_traces)


@pytest.mark.parametrize('message,stop',[('制冰机有焦味',StopReason.ESCALATED),('机器冒烟了',StopReason.CLARIFICATION_REQUESTED)])
async def test_safety(agent,user,sop_service,message,stop):
    result=await agent.handle_turn(request(user,message))
    assert result.stop_reason==stop
    assert '断' in result.answer.splitlines()[0] and '电' in result.answer.splitlines()[0]
    assert '重启' not in result.answer
    assert not sop_service.vector_search.calls
    if stop==StopReason.ESCALATED:
        assert result.escalation_ticket.severity==TicketSeverity.P1_SAFETY
        assert result.escalation_ticket.safety_hazard
        assert '设备编号待现场补录' in result.escalation_ticket.summary
    else:
        assert result.answer.startswith(prompts.SAFETY_CLARIFICATION_PREFIX)


async def test_clarification_limit(agent,user):
    session=None
    for _ in range(3):
        result=await agent.handle_turn(request(user,'制冰机坏了',session))
        session=result.session_id
    assert not result.needs_clarification and result.refused
    assert (await agent.session_store.load(session)).clarification_rounds==2


async def test_low_confidence_and_switch(agent,user,model):
    model.scripts[IntentSlotExtraction].append(IntentSlotExtraction(intent='equipment_fault',confidence=.1,
        slots=ExtractedSlots(equipment_model='制冰机',fault_symptom='不进水')))
    result=await agent.handle_turn(request(user,'制冰机不进水'))
    assert result.needs_clarification and len(result.clarification_slots)==2
    state=await agent.session_store.load(result.session_id)
    merged=agent._merge_extraction(state,extraction(drink_name='鲜柠檬茶'))
    assert merged.clarification_rounds==0 and merged.slots.equipment_model is None
    assert state.slots.equipment_model=='制冰机'


async def test_cross_department_refusal(agent,user,sop_service):
    result=await agent.handle_turn(request(user,'设备维修 2000 元谁审批、看下审批矩阵'))
    assert result.refused and not result.citations
    assert not sop_service.vector_search.calls
    assert not any(k in result.answer for k in ['区域经理','审批矩阵','fin_','财务部'])


async def test_acl_front_filter(agent,user,model):
    model.scripts[IntentSlotExtraction].append(extraction('store_ops_general'))
    user=user.model_copy(update={'dept_id':'supply_chain'})
    outcome=await agent._run_loop(agent._new_session(session_id='s',user_context=user),message='门店手册',trace_id=None)
    assert outcome.response.refused and outcome.generation_contexts==[]


async def test_identity_object_unchanged(agent,user,monkeypatch):
    original=agent.retrieval.search
    state=agent._new_session(session_id='s',user_context=user)
    async def spy(req):
        assert req.user_context is state.user_context
        return await original(req)
    monkeypatch.setattr(agent.retrieval,'search',spy)
    await agent._run_loop(state,message=request(user).message,trace_id=None)


async def test_degraded_and_unavailable(agent,user,sop_service):
    sop_service.keyword_search.delay=.1
    result=await agent.handle_turn(request(user))
    assert result.stop_reason==StopReason.ANSWERED and result.degraded
    assert result.answer.startswith('【风险提示】')
    sop_service.keyword_search.delay=0
    sop_service.keyword_search.fail=RuntimeError('offline')
    sop_service.vector_search.fail=RuntimeError('offline')
    result=await agent.handle_turn(request(user))
    assert result.stop_reason==StopReason.RETRIEVAL_UNAVAILABLE
    trace=next(t for t in result.tool_traces if t.step==LoopStep.RETRIEVE)
    assert trace.error_code=='RETRIEVAL_UNAVAILABLE'
    assert trace.error_hint.startswith('[RETRIEVAL_UNAVAILABLE]')


@pytest.mark.parametrize('decision',['retrieve','made_up_tool'])
async def test_step_limit(agent,user,model,decision):
    agent.config=StoreAgentConfig(max_steps=3)
    model.decision=decision
    result=await agent.handle_turn(request(user))
    assert result.stop_reason==StopReason.MAX_STEPS_REACHED and result.steps_used==3
    assert result.answer==prompts.STEP_LIMIT_FALLBACK
    assert sum(t.step==LoopStep.RETRIEVE for t in result.tool_traces)<=agent.config.max_retrieval_calls


async def test_retrieval_limit_and_illegal_arguments(agent,user,model):
    model.decision='retrieve'
    agent.config=StoreAgentConfig(max_retrieval_calls=1)
    result=await agent.handle_turn(request(user))
    assert result.stop_reason==StopReason.ANSWERED
    assert sum(t.step==LoopStep.RETRIEVE for t in result.tool_traces)==1
    assert any('requested=retrieve; selected=answer' in t.output_summary for t in result.tool_traces)


async def test_ticket_failure(agent,user,tickets):
    tickets.fail=RuntimeError('/secret')
    result=await agent.handle_turn(request(user,'制冰机有焦味'))
    assert result.stop_reason==StopReason.ESCALATED and result.escalation_ticket
    assert result.ticket_receipt is None and '待人工补录' in result.answer
    assert '已提交成功' not in result.answer and '/secret' not in result.model_dump_json()


async def test_sessions_ownership_missing_conflict(agent,user,model):
    result=await agent.handle_turn(request(user,'制冰机坏了'))
    with pytest.raises(SessionOwnershipError):
        await agent.handle_turn(request(user.model_copy(update={'roles':['finance']}),'不进水',result.session_id))
    with pytest.raises(SessionNotFoundError):
        await agent.handle_turn(request(user,session='missing'))
    model.delay=.01
    results=await asyncio.gather(*(agent.handle_turn(request(user,'不进水',result.session_id)) for _ in range(2)),return_exceptions=True)
    assert sum(isinstance(r,SessionConflictError) for r in results)==1
    assert sum(isinstance(r,StoreAgentTurnResponse) for r in results)==1


async def test_cancellation(agent,user,monkeypatch):
    entered=asyncio.Event()
    async def slow(req):
        entered.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(agent.retrieval,'search',slow)
    task=asyncio.create_task(agent.handle_turn(request(user)))
    await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.session_store.save_calls==0


async def test_invoke_isolated_and_unsupported(agent,user,sessions):
    sessions.fail=RuntimeError('must not access sessions')
    invocation=AgentInvocation(trace_id='test:q',scenario='store_sop',question='制冰机坏了',user_context=user)
    result=await agent.invoke(invocation)
    assert result.retrieved_contexts==[]
    assert sessions.load_calls==sessions.save_calls==0
    with pytest.raises(UnsupportedScenarioError):
        await agent.invoke(invocation.model_copy(update={'scenario':'supply_chain'}))


async def test_summary_failure_and_window(agent,user,model):
    from agent_matrix.store_sop.agent import _ConversationSummary
    agent.config=StoreAgentConfig(summary_trigger_turns=2,recent_turns_window=2)
    one=await agent.handle_turn(request(user,'制冰机坏了'))
    model.scripts[_ConversationSummary].append(RuntimeError('summary failure'))
    two=await agent.handle_turn(request(user,'不进水',one.session_id))
    assert two.stop_reason==StopReason.ANSWERED
    assert two.tool_traces[-1].step==LoopStep.SUMMARIZE and two.tool_traces[-1].status==ToolCallStatus.ERROR
    assert len((await agent.session_store.load(one.session_id)).recent_turns)==2


async def test_ticket_and_summary_overlap(agent,user,model,tickets,monkeypatch):
    from agent_matrix.store_sop.agent import _ConversationSummary
    agent.config=StoreAgentConfig(summary_trigger_turns=2)
    one=await agent.handle_turn(request(user,'制冰机坏了'))
    original_model=model.generate_structured
    original_submit=tickets.submit
    summary_started,ticket_started=asyncio.Event(),asyncio.Event()
    async def generate(**kwargs):
        if kwargs['output_model'] is _ConversationSummary:
            summary_started.set()
            await asyncio.wait_for(ticket_started.wait(),1)
        return await original_model(**kwargs)
    async def submit(draft):
        ticket_started.set()
        await asyncio.wait_for(summary_started.wait(),1)
        return await original_submit(draft)
    monkeypatch.setattr(model,'generate_structured',generate)
    monkeypatch.setattr(tickets,'submit',submit)
    two=await agent.handle_turn(request(user,'有焦味',one.session_id))
    assert two.ticket_receipt and summary_started.is_set() and ticket_started.is_set()
    assert two.token_cost==sum(t.token_usage.total_tokens for t in two.tool_traces)


@pytest.mark.parametrize('boundary',['model','session','retrieval','ticket'])
async def test_downstream_errors(agent,user,model,sessions,tickets,monkeypatch,boundary):
    if boundary=='model':
        model.fail=RuntimeError('boom')
        with pytest.raises(ChatModelUnavailableError): await agent.handle_turn(request(user))
    elif boundary=='session':
        sessions.fail=RuntimeError('boom')
        with pytest.raises(SessionStoreUnavailableError): await agent.handle_turn(request(user))
    elif boundary=='retrieval':
        async def fail(req): raise RuntimeError('boom')
        monkeypatch.setattr(agent.retrieval,'search',fail)
        result=await agent.handle_turn(request(user))
        assert result.stop_reason==StopReason.RETRIEVAL_UNAVAILABLE
    else:
        tickets.delay=.05
        agent.config=StoreAgentConfig(ticket_timeout_s=.01)
        result=await agent.handle_turn(request(user,'制冰机有焦味'))
        assert result.ticket_receipt is None


async def test_store_ttl_cas_copy_and_ticket_idempotency(agent,user,sessions,tickets):
    state=agent._new_session(session_id='s',user_context=user)
    saved=await sessions.save(state,expected_version=None,ttl_s=60)
    saved.turn_count=99
    assert (await sessions.load('s')).turn_count==0
    with pytest.raises(SessionConflictError): await sessions.save(state,expected_version=None,ttl_s=60)
    sessions.expires['s']=0
    assert await sessions.load('s') is None
    state=agent._merge_extraction(state,extraction('equipment_fault',equipment_model='制冰机',fault_symptom='不进水'))
    draft=agent._build_ticket_draft(state,safety_hazard=False,citations=[])
    a,b=await asyncio.gather(tickets.submit(draft),tickets.submit(draft))
    assert a.ticket_id==b.ticket_id
    assert {a.status,b.status}=={TicketStatus.CREATED,TicketStatus.DUPLICATE_OPEN}


@pytest.mark.parametrize('answer',['45g [来源1]','柠檬片放45g [来源1]','柠檬片 -40g [来源1]',
    '柠檬片 40G [来源1]','柠檬片 40 g [来源1]','柠檬片 40克 [来源1]','轻压6次 [来源1]'])
async def test_numeric_verbatim_and_drink_row_scope(agent,user,model,answer):
    model.scripts[GroundedAnswerDraft].append(GroundedAnswerDraft(answer=answer,cited_context_indices=[1]))
    result=await agent.handle_turn(request(user))
    assert result.refused and result.citations==[]


async def test_quote_lineage_and_non_cited_numeric_source(agent,user):
    outcome=await agent._run_loop(agent._new_session(session_id='s',user_context=user),message=request(user).message,trace_id=None)
    for citation in outcome.response.citations:
        source=next(c for c in outcome.generation_contexts if c.parent_id==citation.parent_id)
        assert (citation.doc_id,citation.version)==(source.doc_id,source.version)
        assert citation.quote and citation.quote in source.full_content and len(citation.quote)<=200
    citations,trace=agent._verify_citations(GroundedAnswerDraft(answer='45g [来源1]',cited_context_indices=[1]),
        contexts=[chunk(content='柠檬片40g'),chunk('other','other',content='草莓45g')])
    assert citations==[] and trace.status==ToolCallStatus.ERROR


async def test_empty_equipment_evidence_escalates(agent,user):
    user=user.model_copy(update={'dept_id':'no_docs'})
    result=await agent.handle_turn(request(user,'制冰机不进水'))
    assert result.stop_reason==StopReason.ESCALATED and result.escalation_ticket
    assert result.citations==[]


async def test_retrieval_timeout(agent,user,monkeypatch):
    async def slow(req): await asyncio.sleep(1)
    monkeypatch.setattr(agent.retrieval,'search',slow)
    agent.config=StoreAgentConfig(retrieval_timeout_s=.01)
    result=await agent.handle_turn(request(user))
    assert result.stop_reason==StopReason.RETRIEVAL_UNAVAILABLE
    assert result.tool_traces[-1].error_code=='RETRIEVAL_TIMEOUT'


@pytest.mark.parametrize('boundary',['intent','decision','generation','summary','ticket'])
async def test_cancel_other_boundaries(agent,user,model,tickets,monkeypatch,boundary):
    from agent_matrix.store_sop.agent import _ConversationSummary
    entered=asyncio.Event()
    original=model.generate_structured
    async def generate(**kwargs):
        target={'intent':IntentSlotExtraction,'generation':GroundedAnswerDraft,'summary':_ConversationSummary}.get(boundary)
        if kwargs['output_model'] is target:
            entered.set()
            await asyncio.sleep(10)
        return await original(**kwargs)
    async def slow(**kwargs):
        entered.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(model,'generate_structured',generate)
    session=None
    if boundary=='summary':
        agent.config=StoreAgentConfig(summary_trigger_turns=2)
        one=await agent.handle_turn(request(user,'制冰机坏了'))
        session=one.session_id
    if boundary=='decision': monkeypatch.setattr(model,'decide_tool_call',slow)
    if boundary=='ticket':
        async def submit(draft): await slow()
        monkeypatch.setattr(tickets,'submit',submit)
    task=asyncio.create_task(agent.handle_turn(request(user,'制冰机有焦味' if boundary=='ticket' else request(user).message,session)))
    await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task


async def test_budget_reserves_verification(agent,user):
    agent.config=StoreAgentConfig(max_steps=5)
    result=await agent.handle_turn(request(user))
    assert result.stop_reason==StopReason.MAX_STEPS_REACHED and result.steps_used==5
    assert result.tool_traces[-1].status==ToolCallStatus.SKIPPED
    assert result.citations==[]


async def test_long_eval_trace_and_reserved_session(agent,user):
    invocation=AgentInvocation(trace_id='长追踪 /'+('x'*200),scenario='store_sop',question='你好',user_context=user)
    response=await agent.invoke(invocation)
    assert response.refused
    with pytest.raises(SessionNotFoundError):
        await agent.handle_turn(request(user,session='eval:reserved'))


async def test_short_identity_does_not_corrupt_slot_schema(agent,user):
    user=user.model_copy(update={'user_id':'u'})
    first=await agent.handle_turn(request(user,'制冰机坏了'))
    second=await agent.handle_turn(request(user,'不进水',first.session_id))
    assert second.stop_reason==StopReason.ANSWERED
    assert '确认进水阀开启' in second.answer


async def test_model_tool_arguments_cannot_override_identity(agent,user,model,monkeypatch):
    model.scripts[ToolCallDecision].append(ToolCallDecision(tool_name='retrieve',arguments={
        'user_context':{'user_id':'evil','dept_id':'finance','roles':['finance']},'query':'审批矩阵'}))
    original=agent.retrieval.search
    async def spy(req):
        assert req.user_context==user and '审批矩阵' not in req.query
        return await original(req)
    monkeypatch.setattr(agent.retrieval,'search',spy)
    response=await agent.handle_turn(request(user))
    assert response.stop_reason==StopReason.ANSWERED
    assert 'evil' not in response.model_dump_json()
