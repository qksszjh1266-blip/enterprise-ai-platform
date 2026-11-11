import asyncio
import hashlib
import json
import logging
from time import perf_counter
import pytest
from agent_matrix.supply_chain_bi.models import *
from agent_matrix.supply_chain_bi.errors import *
from agent_matrix.supply_chain_bi.render import TRUNCATION_NOTICE
from eval_platform.models import AgentInvocation, EvalScenario
from .fakes import *
from .sim_db import AS_OF, SENTINELS

def request(question='近30天益生菌粉损耗率按仓库排', identity='mgr_east',alerts=False):
    return SupplyChainQueryRequest(question=question,user_context=user(identity),as_of_date=AS_OF,include_alerts=alerts,trace_id='fixed-trace')

async def test_14_loss_normal(make_agent):
    agent=make_agent();r=await agent.analyze(request())
    assert r.stop_reason is StopReason.ANSWERED
    assert dict(r.result_table.rows)['WH_E01']==.05
    assert dict(r.result_table.rows)['WH_E02']==.02
    assert r.narrative_source is NarrativeSource.LLM_VERIFIED
    assert ':scope_region_id' in r.final_sql and 'R_EAST' not in r.final_sql

@pytest.mark.parametrize('kind',['column','execution','scope','literal'])
async def test_15_16_17_24_repair(make_agent,kind):
    good=metric_sql('days_of_supply',ScopeLevel.WAREHOUSE)
    bad={'column':good.replace('i.on_hand_qty','i.loss_id'),'execution':good.replace('i.on_hand_qty','no_such_function(i.on_hand_qty)'), 'scope':good.replace(' AND i.warehouse_id IN :scope_warehouse_ids',''), 'literal':good+" AND i.warehouse_id IN ('WH_E01','WH_E03')"}[kind]
    if kind=='literal':bad=good.replace(' ORDER BY'," AND i.warehouse_id IN ('WH_E01','WH_E03') ORDER BY")
    model=ScriptedSqlModel(candidates=[bad,good]);agent=make_agent(model=model)
    r=await agent.analyze(request('还能撑几天','wh_sup_e01'))
    assert r.stop_reason is StopReason.ANSWERED
    assert len(r.sql_attempts)==2
    first=r.sql_attempts[0]
    assert first.outcome is (AttemptOutcome.EXECUTION_FAILED if kind=='execution' else AttemptOutcome.GUARD_REJECTED)
    assert model.calls['repair_sql'][0]['previous_attempts'][-1].repair_feedback==first.repair_feedback
    if kind!='execution':assert first.repair_feedback.startswith('[SQL_GUARD_REJECTED]')
    if kind=='scope':assert SqlViolationCode.SCOPE_PREDICATE_MISSING in [v.code for v in first.validation.violations]
    if kind=='literal':assert SqlViolationCode.SCOPE_LITERAL_OUT_OF_SCOPE in [v.code for v in first.validation.violations]
    assert {row[0] for row in r.result_table.rows}=={'WH_E01'}

async def test_18_exhausted(make_agent):
    model=ScriptedSqlModel(candidates=['SELECT loss_id FROM fact_material_loss']*3)
    agent=make_agent(model=model);r=await agent.analyze(request())
    assert r.stop_reason is StopReason.SQL_RETRY_EXHAUSTED
    assert len(r.sql_attempts)==3 and not agent.sandbox.calls
    assert len(model.calls['repair_sql'])==2

@pytest.mark.parametrize('sql',['SELECT supplier_cost_price FROM fact_inventory_batch','DROP TABLE dim_material','SELECT * FROM sqlite_master'])
async def test_19_20_fatal(make_agent,caplog,sql):
    model=ScriptedSqlModel(candidates=[sql]);agent=make_agent(model=model)
    r=await agent.analyze(request(alerts=True))
    assert r.stop_reason is StopReason.REFUSED and r.refusal_reason is RefusalReason.POLICY_VIOLATION
    assert len(r.sql_attempts)==1 and not agent.sandbox.calls
    assert not model.calls['repair_sql'] and not model.calls['summarize_analysis']
    assert 'supplier_cost_price' not in r.model_dump_json()+caplog.text
    assert '13.1415' not in r.model_dump_json()+caplog.text

async def test_21_read_only(make_agent,sandbox,caplog):
    spy=SpySandbox(sandbox,status=SqlExecutionStatus.READ_ONLY_VIOLATION)
    agent=make_agent(spy=spy)
    with caplog.at_level(logging.ERROR):r=await agent.analyze(request(alerts=True))
    assert r.refusal_reason is RefusalReason.POLICY_VIOLATION
    assert len(spy.calls)==1
    assert caplog.records and not agent.sql_model.calls['repair_sql']

async def test_22_sensitive_intent(make_agent):
    agent=make_agent();r=await agent.analyze(request('益生菌粉采购底价是多少','proc_mgr',True))
    assert r.refusal_reason is RefusalReason.SENSITIVE_FIELD
    assert not agent.sql_model.calls['generate_sql'] and not agent.sandbox.calls

@pytest.mark.parametrize('q',['全国各仓速溶咖啡粉库存','WH_S01 风味液库存','华南各仓库存'])
async def test_23_scope_intent(make_agent,q):
    agent=make_agent();r=await agent.analyze(request(q,'wh_sup_e01',True))
    assert r.refusal_reason is RefusalReason.SCOPE_VIOLATION
    assert not agent.sandbox.calls

@pytest.mark.parametrize('identity,expected',[('wh_sup_e01',{'WH_E01'}),('mgr_east',{'WH_E01','WH_E02','WH_E03'}),('hq_analyst',{'WH_E01','WH_E02','WH_E03','WH_S01','WH_S02'})])
async def test_25_scope_rows(make_agent,identity,expected):
    agent=make_agent();r=await agent.analyze(request('速溶咖啡粉还能撑几天',identity))
    assert {row[0] for row in r.result_table.rows}==expected
    assert all(c['bind_params']==scope_for(identity).bind_params(as_of_date=AS_OF) for c in agent.sandbox.calls)

async def test_26_alerts(make_agent):
    r=await make_agent().analyze(request(alerts=True))
    alerts={(a.warehouse_id,a.material_id):a for a in r.alerts}
    a=alerts['WH_E01','MAT_COFFEE']
    assert (a.severity,a.recommended_action,a.counterpart_warehouse_id,a.suggested_qty)==(AlertSeverity.P2,AlertAction.TRANSFER_IN,'WH_E03',36)
    a=alerts['WH_E02','MAT_MILK']
    assert (a.severity,a.recommended_action)==(AlertSeverity.P1,AlertAction.REPLENISH)
    a=alerts['WH_E01','MAT_PROBIOTIC']
    assert (a.severity,a.alert_type,a.gap_qty,a.recommended_action)==(AlertSeverity.P1,AlertType.NEAR_EXPIRY_OVERSTOCK,50,AlertAction.TRANSFER_OUT)
    assert alerts['WH_E02','MAT_OAT'].recommended_action is AlertAction.INSPECT_AND_DISPOSE
    assert all(a.warehouse_id.startswith('WH_E') for a in r.alerts)
    assert r.alerts==sorted(r.alerts,key=lambda a:(a.severity.value,a.alert_type.value,a.warehouse_id,a.material_id,a.batch_no or ''))

async def test_27_alert_scope_shrink(make_agent):
    r=await make_agent().analyze(request(identity='wh_sup_e01',alerts=True))
    a=next(a for a in r.alerts if a.material_id=='MAT_COFFEE')
    assert a.recommended_action is AlertAction.REPLENISH
    assert all(a.counterpart_warehouse_id is None and a.warehouse_id=='WH_E01' for a in r.alerts)

async def test_28_parallel(make_agent,sandbox):
    spy=SpySandbox(sandbox,delay=.2);agent=make_agent(spy=spy)
    start=perf_counter();r=await agent.analyze(request(alerts=True));elapsed=perf_counter()-start
    assert r.stop_reason is StopReason.ANSWERED
    assert len(spy.calls)==3
    assert elapsed<.4*.8

async def test_29_alert_failure_isolated(make_agent,sandbox):
    agent=make_agent(spy=SpySandbox(sandbox,alert_fail=True))
    r=await agent.analyze(request(alerts=True))
    assert r.stop_reason is StopReason.ANSWERED and r.alerts==[]

@pytest.mark.parametrize('value,display,source',[(.075,'7.5%',NarrativeSource.TEMPLATE_FALLBACK),(.05,'5.0%',NarrativeSource.LLM_VERIFIED)])
async def test_30_31_numeric_verification(make_agent,value,display,source):
    draft=AnalysisNarrativeDraft(conclusion=f'WH_E01 益生菌粉损耗率 {display}',key_figures=[KeyFigure(label='损耗率',value=value,unit=MetricUnit.RATIO,row_index=0,column='loss_rate')])
    r=await make_agent(model=ScriptedSqlModel(narratives=[draft])).analyze(request())
    assert r.narrative_source is source
    assert '7.5' not in r.conclusion
    assert ('5.00%' in r.conclusion) if source is NarrativeSource.TEMPLATE_FALLBACK else ('5.0%' in r.conclusion)

async def test_32_clarification(make_agent):
    model=ScriptedSqlModel(intents=[AnalysisIntent(intent_type='metric_query',confidence=.3,metric_ids=['material_loss_rate'])])
    r=await make_agent(model=model).analyze(request())
    assert r.stop_reason is StopReason.CLARIFICATION_REQUESTED and not model.calls['generate_sql']

async def test_33_capped_result(make_agent):
    sql='SELECT s.store_id,s.cups_sold FROM fact_store_sales_daily s WHERE s.region_id=:scope_region_id ORDER BY s.store_id,s.biz_date,s.sku_id'
    r=await make_agent(model=ScriptedSqlModel(candidates=[sql]),max_result_rows=5).analyze(request('店铺销量'))
    assert r.result_table.source_truncated and len(r.result_table.rows)==5
    assert TRUNCATION_NOTICE.format(max_rows=5) in r.conclusion

async def test_34_source_unavailable(make_agent,sandbox):
    agent=make_agent(spy=SpySandbox(sandbox,fail=SandboxUnavailableError('source down')))
    r=await agent.analyze(request())
    assert r.stop_reason is StopReason.DATA_SOURCE_UNAVAILABLE and r.final_sql is None

@pytest.mark.parametrize('identity,fail,error',[('mgr_east',RuntimeError('down'),ScopeResolutionError),('clerk_01',None,ScopeDeniedError)])
async def test_35_scope_fail_closed(make_agent,identity,fail,error):
    agent=make_agent(resolver=StaticScopeResolver(fail=fail))
    with pytest.raises(error):await agent.analyze(request(identity=identity))
    assert not agent.sandbox.calls and not agent.sql_model.calls

async def test_35_scope_identity_mismatch(make_agent):
    class Mismatched(StaticScopeResolver):
        async def resolve(self,user_context):return scope_for('hq_analyst')
    agent=make_agent(resolver=Mismatched())
    with pytest.raises(ScopeResolutionError):await agent.analyze(request())
    assert not agent.sandbox.calls and not agent.sql_model.calls

@pytest.mark.parametrize('stage',['generate_sql','summarize_analysis'])
async def test_36_model_timeout(make_agent,stage):
    agent=make_agent(model=ScriptedSqlModel(delay={stage:.1}),model_timeout_s=.01)
    if stage=='generate_sql':
        with pytest.raises(SqlModelUnavailableError):await agent.analyze(request())
    else:
        r=await agent.analyze(request())
        assert r.narrative_source is NarrativeSource.TEMPLATE_FALLBACK and r.token_cost==30

async def test_37_cancel(make_agent,sandbox):
    agent=make_agent(spy=SpySandbox(sandbox,delay=2))
    task=asyncio.create_task(agent.analyze(request(alerts=True)))
    await asyncio.sleep(.05);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task

async def test_38_invariants(make_agent,db_path,caplog):
    digest=hashlib.sha256(db_path.read_bytes()).hexdigest()
    agent=make_agent();req=request(alerts=True);r=await agent.analyze(req)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest()==digest
    assert len(r.sql_attempts)<=agent.config.max_sql_attempts
    assert [a.attempt_index for a in r.sql_attempts]==list(range(1,len(r.sql_attempts)+1))
    assert sum(len(c) for c in agent.sql_model.calls.values())<=1+agent.config.max_sql_attempts+1
    assert r.token_cost==r.token_usage.total_tokens==45
    for call in agent.sandbox.calls:
        v=agent.guard.validate(call['sql'],scope=scope_for())
        assert v.is_valid and v.normalized_sql==call['sql']
        assert call['sql'].endswith('LIMIT 500')
        assert call['bind_params']==scope_for().bind_params(as_of_date=AS_OF)
    invocation=AgentInvocation(trace_id='eval',scenario=EvalScenario.SUPPLY_CHAIN,question=req.question,user_context=req.user_context)
    mapped=await agent.invoke(invocation)
    serialized=r.model_dump_json()+mapped.model_dump_json()+caplog.text+repr(agent.sql_model.calls)
    for secret in [*SENTINELS,*agent.semantic_layer.catalog.global_sensitive_columns,*agent.semantic_layer.catalog.forbidden_tables]:
        assert secret not in serialized
    assert 'WH_S01' not in serialized and 'WH_S02' not in serialized
    r2=await make_agent().analyze(req)
    def stable(value):
        if isinstance(value,dict):return {k:stable(v) for k,v in value.items() if k not in ('latency_ms','stage_latency_ms','elapsed_ms')}
        if isinstance(value,list):return [stable(v) for v in value]
        return value
    assert stable(r.model_dump())==stable(r2.model_dump())

async def test_unsupported_scenario(make_agent):
    agent=make_agent()
    with pytest.raises(UnsupportedScenarioError):
        await agent.invoke(AgentInvocation(trace_id='bad',scenario=EvalScenario.STORE_SOP,question='hello',user_context=user()))

async def test_fatal_repair_cancels_inflight_alerts(make_agent,sandbox):
    class SlowAlerts(SpySandbox):
        def __init__(self):
            super().__init__(sandbox)
            self.cancelled=0
        async def execute(self,**kwargs):
            if 'AS available_qty' in kwargs['sql'] or 'AS days_to_expiry' in kwargs['sql']:
                try:await asyncio.sleep(2)
                except asyncio.CancelledError:
                    self.cancelled+=1
                    raise
            return await super().execute(**kwargs)
    sql=metric_sql('days_of_supply',ScopeLevel.REGION).replace('i.on_hand_qty','no_such_function(i.on_hand_qty)')
    spy=SlowAlerts()
    agent=make_agent(spy=spy,model=ScriptedSqlModel(candidates=[sql,'SELECT supplier_cost_price FROM fact_inventory_batch']))
    start=perf_counter();r=await agent.analyze(request('还能撑几天',alerts=True))
    assert r.refusal_reason is RefusalReason.POLICY_VIOLATION
    assert spy.cancelled==2 and perf_counter()-start<1
    assert len(agent.sql_model.calls['repair_sql'])==1
    assert r.stage_latency_ms[LoopStage.GENERATE]>0
    assert r.stage_latency_ms[LoopStage.REPAIR]>0
    assert r.stage_latency_ms[LoopStage.GUARD]>0
    assert r.stage_latency_ms[LoopStage.EXECUTE]>0
