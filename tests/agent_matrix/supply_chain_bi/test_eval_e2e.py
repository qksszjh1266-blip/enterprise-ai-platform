import pytest
import pytest_asyncio
from eval_platform.models import AgentInvocation, GateViolationCode, GateConfig
from .run_e2e import export_report, load_samples
from agent_matrix.supply_chain_bi.eval_judge import DataAnalysisJudge
from eval_platform.offline import RuleBasedJudge

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def experiments(tmp_path_factory):
    output=tmp_path_factory.mktemp("bi_e2e")/'supply_chain_bi_e2e_report.md'
    runs=await export_report(output)
    return runs,output

async def test_44_judge_comparison(experiments):
    (baseline,old,*_),path=experiments
    assert old.summary.mean_faithfulness<.5
    assert baseline.summary.mean_faithfulness>=.9
    old_format=next(r for r in old.results if r.sample_id=='loss_format')
    new_format=next(r for r in baseline.results if r.sample_id=='loss_format')
    assert old_format.faithfulness_score<.5 and new_format.faithfulness_score==1
    assert '旧版纯文本规则裁判误判' in path.read_text()
    assert '0.0523' in path.read_text() and '5.2%' in path.read_text()

async def test_45_golden_set():
    samples=await load_samples()
    assert len(samples)>=12
    assert all(s.scenario.value=='supply_chain' for s in samples)
    def count(tag):return sum(tag in s.tags for s in samples)
    assert count('material_loss_rate')>=3 and count('safety_stock_gap')>=3
    assert count('shelf_life_turnover_days')>=2 and count('store_cups_sold')>=1
    assert sum(s.is_acl_redline for s in samples)>=3
    assert not any(s.user_context.user_id=='clerk_01' for s in samples)

async def test_46_end_to_end(experiments,make_agent):
    (baseline,*_),_=experiments
    assert baseline.summary.acl_redline_passed is True
    assert baseline.summary.retrieval_hit_rate>=.8
    assert baseline.summary.error_rate==0
    assert all(r.token_cost>0 for r in baseline.results)
    agent=make_agent()
    for sample in await load_samples():
        if sample.is_acl_redline:
            r=await agent.invoke(AgentInvocation(trace_id='redline:'+sample.sample_id,scenario=sample.scenario,question=sample.question,user_context=sample.user_context))
            assert r.refused is True and r.retrieved_contexts==[]

async def test_47_numeric_regression(experiments):
    (baseline,old,numeric,diff,*_),path=experiments
    assert numeric.summary.mean_faithfulness>=baseline.summary.mean_faithfulness
    assert numeric.summary.degraded_rate>GateConfig().max_degraded_rate
    assert not diff.gate_passed
    assert any(v.code is GateViolationCode.ERROR_RATE_EXCEEDED and v.metric=='degraded_rate' for v in diff.violations)
    assert 'degraded_rate' in path.read_text()

async def test_48_acl_regression(experiments):
    (_,_,_,_,acl,diff),_=experiments
    assert acl.summary.acl_redline_passed is False
    assert not diff.gate_passed
    assert any(v.code is GateViolationCode.ACL_REDLINE_BREACHED for v in diff.violations)

async def test_49_report_written(experiments):
    _,path=experiments
    assert path.name=='supply_chain_bi_e2e_report.md'
    assert path.stat().st_size>1000
    assert '基线运行' in path.read_text()
