import pytest
from eval_platform.models import GateConfig, GateViolationCode as V, FailureCategory as F
from eval_platform.runner import RegressionGate
from tests.eval_platform.conftest import record, result


def codes(diff):
    return [v.code for v in diff.violations]


@pytest.mark.parametrize('changes,want',[
    (dict(acl_redline_total=0,acl_redline_blocked=0,acl_redline_passed=False,acl_block_rate=None), V.ACL_REDLINE_BREACHED),
    (dict(mean_mrr=.5), V.METRIC_REGRESSION),
    (dict(retrieval_hit_rate=.7), V.ABSOLUTE_FLOOR),
    (dict(latency_p95_ms=13), V.LATENCY_REGRESSION),
    (dict(degraded_rate=.1), V.ERROR_RATE_EXCEEDED),
    (dict(error_rate=.1), V.ERROR_RATE_EXCEEDED),
    (dict(mean_mrr=None), V.METRIC_UNAVAILABLE),
])
def test_g1_g3_to_g7(changes,want):
    diff=RegressionGate().compare(record(),record(**changes))
    assert want in codes(diff) and not diff.gate_passed


def test_g2_skips_g3_to_g8_but_not_redline():
    base=record()
    candidate=record([result('s1',failure_category=F.SYSTEM_ERROR)],retrieval_hit_rate=0)
    candidate.suite_fingerprint='changed'
    diff=RegressionGate().compare(base,candidate)
    assert codes(diff)==[V.ACL_REDLINE_BREACHED,V.SUITE_MISMATCH]
    assert diff.metric_deltas==[] and not diff.suite_comparable
    override=RegressionGate(GateConfig(require_same_suite=False)).compare(base,candidate)
    assert override.metric_deltas and V.ACL_REDLINE_BREACHED in codes(override)


def test_g8_new_failures():
    base=record()
    candidate=record([result('s1',failure_category=F.JUDGE_ERROR),result('acl',is_acl_redline=True)])
    diff=RegressionGate().compare(base,candidate)
    assert codes(diff)==[V.METRIC_REGRESSION]
    assert diff.violations[0].metric is None


def test_g3_quality_metrics_and_order():
    b=record(answer_accuracy_rate=1,mean_faithfulness=1,mean_satisfaction=5)
    c=record(retrieval_hit_rate=.5,mean_recall_at_k=.5,mean_mrr=.5,answer_accuracy_rate=.5,mean_faithfulness=.5,mean_satisfaction=2,
        latency_p95_ms=30,degraded_rate=.5,error_rate=.5)
    diff=RegressionGate().compare(b,c)
    assert codes(diff)==[V.METRIC_REGRESSION]*6+[V.ABSOLUTE_FLOOR]*2+[V.LATENCY_REGRESSION]+[V.ERROR_RATE_EXCEEDED]*2
    assert [v.metric for v in diff.violations[:6]]==[name for name,_ in RegressionGate.COMPARED_METRICS[:6]]
    assert [d.metric for d in diff.metric_deltas]==[name for name,_ in RegressionGate.COMPARED_METRICS]


@pytest.mark.parametrize('drop,regressed',[(.03,False),(.0300000001,False),(.030001,True)])
def test_tolerance_boundary(drop,regressed):
    diff=RegressionGate().compare(record(),record(mean_mrr=1-drop))
    assert (V.METRIC_REGRESSION in codes(diff))==regressed


def test_equal_passes_and_zero_relative_delta():
    diff=RegressionGate().compare(record(),record())
    assert diff.gate_passed and not diff.violations
    assert next(d for d in diff.metric_deltas if d.metric=='error_rate').rel_delta_pct is None
    assert next(d for d in diff.metric_deltas if d.metric=='mean_mrr').rel_delta_pct==0
    delta=next(d for d in RegressionGate().compare(record(),record(mean_mrr=.8)).metric_deltas if d.metric=='mean_mrr')
    assert delta.abs_delta==pytest.approx(-.2) and delta.rel_delta_pct==pytest.approx(-20)


def test_absolute_latency_limit_and_floors_boundary():
    gate=RegressionGate(GateConfig(max_p95_latency_ms=10))
    assert gate.compare(record(),record()).gate_passed
    assert V.LATENCY_REGRESSION in codes(gate.compare(record(),record(latency_p95_ms=10.01)))
    same=record(retrieval_hit_rate=.8,mean_faithfulness=.85)
    assert gate.compare(same,same).gate_passed


def test_sample_diff_bad_cases_severity_and_cap():
    base=record([result('fixed',failure_category=F.RETRIEVAL_MISS),result('new'),result('old',failure_category=F.JUDGE_ERROR),result('a')])
    candidate=record([result('fixed'),result('new',failure_category=F.RETRIEVAL_MISS),result('old',failure_category=F.JUDGE_ERROR),
        result('a',failure_category=F.ACL_LEAK,acl_leaked=True),result('unpaired',failure_category=F.TIMEOUT)])
    diff=RegressionGate(GateConfig(top_bad_cases=3)).compare(base,candidate)
    assert diff.newly_failed_sample_ids==['a','new'] and diff.fixed_sample_ids==['fixed']
    assert [b.sample_id for b in diff.top_bad_cases]==['a','unpaired','new']
    assert [b.severity for b in diff.top_bad_cases]==[115,80,75]
    assert diff.top_bad_cases[1].baseline_category is None
    assert RegressionGate(GateConfig(top_bad_cases=0)).compare(base,candidate).top_bad_cases==[]


def test_no_redline_cannot_be_waived():
    run=record([result()])
    diff=RegressionGate(GateConfig(require_same_suite=False,max_error_rate=1,max_degraded_rate=1)).compare(run,run)
    assert codes(diff)==[V.ACL_REDLINE_BREACHED]
