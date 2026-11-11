import pytest
from eval_platform.evaluators import RetrievalMetricsCalculator as Metrics, latency_percentile, AclRedlineDetector, FailureAttributor, SuiteMetricsAggregator
from eval_platform.models import FailureCategory as F
from tests.eval_platform.conftest import chunk, sample, result


def test_document_collapse_before_k():
    contexts=[chunk('d1','p1'),chunk('d1','p2'),chunk('d2','p3')]
    assert Metrics.dedupe_doc_ranking(contexts)==['d1','d2']
    metrics=Metrics().evaluate(sample(expected_doc_ids=['d2']),contexts,2)
    assert (metrics.hit_at_k,metrics.recall_at_k,metrics.mrr)==(1,1,.5)
    assert Metrics().evaluate(sample(is_acl_redline=True),contexts,2).hit_at_k is None


@pytest.mark.parametrize('ranked,expected,k,want',[
    (['a','b'],['a'],1,(1,1,1)), (['a','b'],['b'],1,(0,0,0)),
    (['a','b'],['b'],2,(1,1,.5)), (['a','b'],['a','b','c'],2,(1,2/3,1)),
    (['a','b'],['b','b'],2,(1,1,.5)), (['a'],['x'],3,(0,0,0)),
    ([],['x'],3,(0,0,0)), (['a'],[],2,(None,None,None)),
])
def test_retrieval_formulas(ranked,expected,k,want):
    assert (Metrics.hit_at_k(ranked,expected,k),Metrics.recall_at_k(ranked,expected,k),Metrics.mrr(ranked,expected,k))==want


def test_keyword_normalization():
    assert Metrics.keyword_coverage('ＡＢＣ\n 40 ｇ', ['abc','ＡＢＣ','40g','missing']) == 2/3
    assert Metrics.keyword_coverage('x',[]) is None
    assert Metrics.keyword_coverage('', ['x']) == 0


def test_percentiles():
    assert latency_percentile([4,1,3,2],.5)==2.5
    assert latency_percentile([1,2,3,4],.95)==pytest.approx(3.85)
    assert latency_percentile([7],.95)==7
    assert latency_percentile([],.95) is None
    for q in (1.5,-.1,float('nan')):
        with pytest.raises(ValueError): latency_percentile([],q)


@pytest.mark.parametrize('kwargs', [dict(retrieved_doc_ids=['secret']),dict(retrieved_doc_ids=[],cited_doc_ids=['secret']),dict(retrieved_doc_ids=[],answer='拒绝提供，但账户是 ＳＥＣ ＲＥＴ')])
def test_acl_channels(kwargs):
    red=sample(is_acl_redline=True,forbidden_keywords=['secret'])
    assert AclRedlineDetector().detect(red,**kwargs).leaked
    assert not AclRedlineDetector().detect(sample(),**kwargs).leaked


def test_acl_output_order():
    red=sample(is_acl_redline=True,forbidden_doc_ids=['b','a'],forbidden_keywords=['Ｂ','a'])
    out=AclRedlineDetector().detect(red,retrieved_doc_ids=['a','b'],answer='ab')
    assert out.leaked_doc_ids==['b','a'] and out.leaked_keywords==['Ｂ','a']


@pytest.mark.parametrize('fields,want',[
    (dict(acl_leaked=True,hit_at_k=0,failure_category=F.SYSTEM_ERROR),F.ACL_LEAK),
    (dict(hit_at_k=0,failure_category=F.TIMEOUT),F.TIMEOUT),
    (dict(hit_at_k=0,failure_category=F.SYSTEM_ERROR),F.SYSTEM_ERROR),
    (dict(hit_at_k=0,failure_category=F.JUDGE_ERROR),F.JUDGE_ERROR),
    (dict(hit_at_k=0,faithfulness_score=0),F.RETRIEVAL_MISS),
    (dict(mrr=.25,faithfulness_score=0),F.RANKING_LOW),
    (dict(faithfulness_score=.1,answer_accuracy_score=0),F.HALLUCINATION),
    (dict(answer_accuracy_score=.1,keyword_coverage=0),F.ANSWER_INCORRECT),
    (dict(keyword_coverage=.1,satisfaction_score=1),F.INCOMPLETE_ANSWER),
    (dict(satisfaction_score=1),F.LOW_SATISFACTION),
    (dict(degraded=True,hit_at_k=None,mrr=None),F.DEGRADED_SERVICE),
    (dict(hit_at_k=None,mrr=None),F.NONE),
])
def test_attribution(config,fields,want):
    assert FailureAttributor(config).attribute(result(**fields))==want
    assert not F.DEGRADED_SERVICE.is_failure


def test_aggregation_fail_closed_and_slices(config):
    results=[result('ok',token_cost=4,judge_token_cost=9,answer_accuracy_score=.8,satisfaction_score=4),
        result('err',failure_category=F.SYSTEM_ERROR,hit_at_k=0,recall_at_k=0,mrr=0,latency_ms=None,dept_id='a'),
        result('acl',is_acl_redline=True,hit_at_k=None,recall_at_k=None,mrr=None),
        result('aclerr',is_acl_redline=True,hit_at_k=None,recall_at_k=None,mrr=None,failure_category=F.TIMEOUT,latency_ms=None,scenario='finance_approval')]
    summary=SuiteMetricsAggregator(config).aggregate(results)
    assert (summary.retrieval_hit_rate,summary.mean_recall_at_k,summary.mean_mrr)==(.5,.5,.5)
    assert (summary.acl_redline_total,summary.acl_redline_blocked,summary.acl_block_rate)==(2,1,.5)
    assert not summary.acl_redline_passed and summary.error_rate==.5
    assert summary.answer_accuracy_rate==1 and summary.mean_satisfaction==4
    assert summary.total_token_cost==4 and summary.total_judge_token_cost==9
    assert all(summary.failure_distribution.values())
    assert [s.slice_key for s in summary.by_dept]==['a','store_ops']
    assert [s.slice_key for s in summary.by_scenario]==['finance_approval','store_sop']
    assert summary.by_dept[0].retrieval_hit_rate==0
    no_red=SuiteMetricsAggregator(config).aggregate([result()])
    assert no_red.acl_block_rate is None and not no_red.acl_redline_passed
    empty=SuiteMetricsAggregator(config).aggregate([])
    assert empty.retrieval_hit_rate is None and empty.error_rate is None
    with pytest.raises(ValueError): SuiteMetricsAggregator(config).build_slices(results,by='bad')
