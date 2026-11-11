"""Retrieval baseline experiments using production services and fake storage/search ports."""
import pytest
from data_platform.retrieval_engine.models import RetrievalBranch, RetrievalCandidate, SearchRequest
from eval_platform.models import EvalRunConfig, GateConfig, GateViolationCode as V, FailureCategory as F
from eval_platform.ports import RetrievalSystemPort
from eval_platform.runner import EvalRunner, RegressionGate
from eval_platform.report_generator import MarkdownReportGenerator, RECOMMENDATION_RULES
from tests.eval_platform.conftest import sample
from tests.eval_platform.corpus import publish_corpus


@pytest.fixture
async def experiment(manager,retrieval):
    await publish_corpus(manager)
    # Three normal questions for each business scenario, labelled from the original SOP only.
    # Amounts are written in Chinese numerals: the char-count FakeReranker otherwise rewards the digit-dense SKU spec table.
    definitions=[
        ('store_sop','蛋白粉随行杯蛋白粉多少克','sop_sku_spec',['40g'],['table']),
        ('store_sop','热敏打单机不出纸如何检查','sop_equipment',['纸仓','面单规格'],['page_2']),
        ('store_sop','燕麦拿铁随行杯燕麦颗粒多少克','sop_sku_spec',['50g'],['table']),
        ('supply_chain','益生菌冲饮每件益生菌粉多少克','sop_sku_spec',['45g'],['table']),
        ('supply_chain','商品使用前如何核对批号与有效期','sop_sku_spec',['批号','有效期'],[]),
        ('supply_chain','含乳成分商品能未经批准替换商品吗','sop_sku_spec',['未经批准'],[]),
        ('finance_approval','设备维修两千元谁审批','fin_approval_matrix',['区域经理','财务'],['table']),
        ('finance_approval','日常耗材五百元以内谁审批','fin_approval_matrix',['运营主管'],['table']),
        ('finance_approval','申请人能审批本人报销吗','fin_expense_policy',['不得审批本人'],[]),
    ]
    samples=[]
    for i,(scenario,q,doc,kws,tags) in enumerate(definitions):
        s=sample(f'n{i}',scenario=scenario,question=q,expected_doc_ids=[doc],expected_keywords=kws,tags=tags)
        if doc=='fin_approval_matrix':
            s=s.model_copy(update={'user_context':s.user_context.model_copy(update={'roles':['store_manager']})})
        samples.append(s)
    for i,q in enumerate(['导出审批矩阵','他人银行账户是什么','请提供合同价格']):
        samples.append(sample(f'acl{i}',scenario='finance_approval',question=q,is_acl_redline=True,forbidden_doc_ids=['fin_approval_matrix']))
    red=sample('cross',scenario='supply_chain',question='店铺内部规格参数',is_acl_redline=True,forbidden_doc_ids=['sop_sku_spec','sop_equipment'])
    samples.append(red.model_copy(update={'user_context':red.user_context.model_copy(update={'dept_id':'supply_chain'})}))
    assert isinstance(retrieval,RetrievalSystemPort)
    return retrieval,samples


def run_config(rerank=True):
    return EvalRunConfig(system_name='HybridRetrievalService',system_version='rerank' if rerank else 'rrf',top_k=5,enable_rerank=rerank)


def experiment_gate():
    # Local SQLite scheduling is not a load test. Absolute 1s budget remains enforced.
    return RegressionGate(GateConfig(max_p95_latency_increase=10,max_p95_latency_ms=1000))


async def evaluate(service,samples,rerank=True):
    return await EvalRunner(config=run_config(rerank),retrieval=service).run(samples,suite_name='retrieval-sop')


async def test_e1_rerank_benefit(experiment,tmp_path):
    service,samples=experiment
    baseline=await evaluate(service,samples,False)
    candidate=await evaluate(service,samples)
    diff=experiment_gate().compare(baseline,candidate)
    assert candidate.summary.mean_mrr>=baseline.summary.mean_mrr
    assert diff.gate_passed, diff.blocking_reasons
    assert service.reranker.calls
    for s in samples:
        if 'page_2' in s.tags:
            response=await service.search(SearchRequest(query=s.question,user_context=s.user_context))
            assert any(p.doc_id=='sop_equipment' and p.page_number==2 for p in response.results)
    renderer=MarkdownReportGenerator()
    report=renderer.render(candidate,diff)
    assert '✅ 通过' in report
    await renderer.write(tmp_path/'e1_report.md',report)
    assert (tmp_path/'e1_report.md').read_text()==report


async def test_e2_keyword_timeout(experiment):
    service,samples=experiment
    baseline=await evaluate(service,samples)
    service.keyword_search.delay=.1
    candidate=await evaluate(service,samples)
    diff=experiment_gate().compare(baseline,candidate)
    assert V.ERROR_RATE_EXCEEDED in [v.code for v in diff.violations]
    assert candidate.summary.degraded_rate>.05
    assert 'degraded_rate' in MarkdownReportGenerator().render(candidate,diff)


async def test_e3_both_branches_fail(experiment):
    service,samples=experiment
    baseline=await evaluate(service,samples)
    service.keyword_search.fail=RuntimeError('keyword offline')
    service.vector_search.fail=RuntimeError('vector offline')
    candidate=await evaluate(service,samples)
    # No-visible-doc requests short-circuit before either branch, per the retrieval contract.
    assert all(r.failure_category==F.SYSTEM_ERROR for r in candidate.results if r.sample_id!='cross')
    assert candidate.results[-1].failure_category==F.NONE
    assert candidate.summary.retrieval_hit_rate==0
    diff=experiment_gate().compare(baseline,candidate)
    assert {V.METRIC_REGRESSION,V.ERROR_RATE_EXCEEDED,V.ACL_REDLINE_BREACHED}<={v.code for v in diff.violations}
    assert RECOMMENDATION_RULES[F.SYSTEM_ERROR] in MarkdownReportGenerator().render(candidate,diff)


async def test_e4_acl_injection(experiment,manager,monkeypatch):
    service,samples=experiment
    baseline=await evaluate(service,samples)
    child=next(c for c in manager.vector_store.data.values() if c.doc_id=='fin_approval_matrix')
    service.vector_search.leak=[RetrievalCandidate(child_id=child.child_id,parent_id=child.parent_id,
        doc_id=child.doc_id,version=1,branch=RetrievalBranch.VECTOR,raw_score=999,rank=1)]
    # Confirm the real defense still blocks an adapter leak first.
    safe=await evaluate(service,samples)
    assert safe.summary.acl_redline_passed
    monkeypatch.setattr(service,'_enforce_filter',lambda candidates, retrieval_filter:list(candidates))
    candidate=await evaluate(service,samples)
    diff=experiment_gate().compare(baseline,candidate)
    assert diff.violations[0].code==V.ACL_REDLINE_BREACHED
    assert diff.top_bad_cases[0].failure_category==F.ACL_LEAK
    assert 'ACL 红线未通过' in MarkdownReportGenerator().render(candidate,diff)


async def test_e5_suite_changed(experiment):
    service,samples=experiment
    baseline=await evaluate(service,samples)
    changed=[samples[0].model_copy(update={'question':'改写问题'}),*samples[1:]]
    candidate=await evaluate(service,changed)
    diff=experiment_gate().compare(baseline,candidate)
    assert [v.code for v in diff.violations]==[V.SUITE_MISMATCH]
    assert diff.metric_deltas==[]
    assert '样本集指纹不一致' in MarkdownReportGenerator().render(candidate,diff)
