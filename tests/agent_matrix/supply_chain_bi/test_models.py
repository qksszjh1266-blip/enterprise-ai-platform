"""Model validators and protocol boundaries."""
import pytest
from pydantic import ValidationError
from datetime import datetime
from agent_matrix.supply_chain_bi.models import *
from agent_matrix.supply_chain_bi.ports import *
from agent_matrix.supply_chain_bi.semantic_layer import SUPPLY_CHAIN_CATALOG
from agent_matrix.supply_chain_bi.eval_judge import DataAnalysisJudge
from agent_matrix.supply_chain_bi.errors import *
from eval_platform.ports import AgentSystemPort, LLMJudgePort
from .fakes import *
from .sim_db import AS_OF

@pytest.mark.parametrize('update',[
 {'refused':True},{'final_sql':None},{'result_table':None},{'narrative_source':'not_applicable'},
 {'token_cost':999},{'stop_reason':'sql_retry_exhausted'},{'refusal_reason':'policy_violation'},
 {'sql_attempts':[]},
])
async def test_01_response_invalid(make_agent,update):
    r=await make_agent().analyze(SupplyChainQueryRequest(question='损耗率',user_context=user(),as_of_date=AS_OF,include_alerts=False))
    data=r.model_dump();data.update(update)
    with pytest.raises(ValidationError):SupplyChainQueryResponse.model_validate(data)

async def test_01_noncontinuous(make_agent):
    r=await make_agent().analyze(SupplyChainQueryRequest(question='损耗率',user_context=user(),as_of_date=AS_OF,include_alerts=False))
    data=r.model_dump();data['sql_attempts'][0]['attempt_index']=2
    with pytest.raises(ValidationError):SupplyChainQueryResponse.model_validate(data)

@pytest.mark.parametrize('identity,update',[
 ('wh_sup_e01',{'authorized_warehouse_ids':[]}),('mgr_east',{'authorized_warehouse_ids':['WH_E01']}),
 ('hq_analyst',{'region_id':'R_EAST'}),('wh_sup_e01',{'authorized_warehouse_ids':['WH_E02','WH_E01']}),
 ('wh_sup_e01',{'resolved_at':datetime(2026,11,1)}),
])
def test_02_scope_validation(identity,update):
    data=scope_for(identity).model_dump();data.update(update)
    with pytest.raises(ValidationError):WarehouseScopeFilter.model_validate(data)

def test_02_bind_params_copy():
    for name in ('wh_sup_e01','mgr_east','hq_analyst'):
        s=scope_for(name);params=s.bind_params(as_of_date=AS_OF)
        assert params['as_of_date']=='2026-11-01'
        assert ('scope_region_id' in params)==(name!='hq_analyst')
        if name=='wh_sup_e01':
            params['scope_warehouse_ids'].append('WH_S01')
            assert s.authorized_warehouse_ids==['WH_E01']

def test_03_execution_and_validation_models():
    SqlExecutionResult(status='success',elapsed_ms=0)
    invalid=[{'status':'success','rows':[[1]],'row_count':0}, {'status':'success','rows':[[1]],'row_count':1}, {'status':'success','error_hint':'x'}, {'status':'type_error'}, {'status':'type_error','rows':[[1]],'error_hint':'x'}]
    for data in invalid:
        with pytest.raises(ValidationError):SqlExecutionResult(elapsed_ms=0,**data)
    invalid=[{'is_valid':True},{'is_valid':False},{'is_valid':False,'error_hint':'x','violations':[]}, {'is_valid':False,'fatal':False,'error_hint':'x','violations':[{'code':'sensitive_column','message':'blocked'}]}]
    for data in invalid:
        with pytest.raises(ValidationError):SqlValidationResult(original_sql='SELECT 1',**data)

async def test_03_attempt_and_alert_models(make_agent):
    r=await make_agent().analyze(SupplyChainQueryRequest(question='损耗率',user_context=user(),as_of_date=AS_OF))
    a=r.sql_attempts[0].model_dump()
    for update in ({'outcome':'guard_rejected'},{'execution':None},{'outcome':'execution_failed'}):
        with pytest.raises(ValidationError):SqlAttempt.model_validate(a|update)
    alert=r.alerts[0].model_dump()
    for update in ({'batch_no':None},{'expiry_date':None},{'counterpart_warehouse_id':alert['warehouse_id']},{'recommended_action':'replenish'}):
        with pytest.raises(ValidationError):InventoryAlert.model_validate(alert|update)

def test_04_catalog_validators():
    assert SUPPLY_CHAIN_CATALOG.catalog_version==1
    data=SUPPLY_CHAIN_CATALOG.model_dump();data['tables'][0]['sensitive_columns']=['warehouse_id']
    with pytest.raises(ValidationError):SemanticCatalogSpec.model_validate(data)
    data=SUPPLY_CHAIN_CATALOG.model_dump();data['metrics'][0]['required_columns']['fact_material_loss'].append('not_registered')
    with pytest.raises(ValidationError):SemanticCatalogSpec.model_validate(data)

def test_05_protocols(make_agent,sandbox):
    assert isinstance(ScriptedSqlModel(),SqlGeneratorModelPort)
    assert isinstance(StaticScopeResolver(),WarehouseScopeResolverPort)
    assert isinstance(sandbox,ReadOnlySqlSandboxPort)
    assert isinstance(SpySandbox(sandbox),ReadOnlySqlSandboxPort)
    assert isinstance(make_agent(),AgentSystemPort)
    assert isinstance(DataAnalysisJudge(),LLMJudgePort)

@pytest.mark.parametrize('cls',[SupplyChainAgentError,ScopeDeniedError,ScopeResolutionError,SqlModelUnavailableError,StructuredOutputError,SandboxUnavailableError,SemanticCatalogError,AgentNotInitializedError,UnsupportedScenarioError])
def test_error_hint(cls):
    e=cls('可修正信息');assert e.to_hint()==f'[{e.code}] trace_id=- | 可修正信息 | 建议: {e.suggestion}'
    assert 'trace_id=trace' in cls('x',trace_id='trace').to_hint()

def test_model_identity_injection():
    with pytest.raises(ValidationError):AnalysisIntent(intent_type='metric_query',confidence=1,region_id='R_SOUTH')
    with pytest.raises(ValidationError):SqlCandidate(sql='SELECT 1',scope={})
    with pytest.raises(ValidationError):AnalysisNarrativeDraft(conclusion='hi',user_id='evil')
