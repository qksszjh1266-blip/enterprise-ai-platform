import pytest
from pydantic import ValidationError
from eval_platform.models import EvalSample, SuiteSummaryMetrics, RegressionDiffReport, GateViolation, GateViolationCode
from eval_platform.ports import RetrievalSystemPort, AgentSystemPort, LLMJudgePort
from tests.eval_platform.conftest import sample
from tests.eval_platform.fakes import FakeRetrievalSystem, FakeAgentSystem, FakeJudge


@pytest.mark.parametrize('updates', [
    dict(is_acl_redline=True, forbidden_doc_ids=['x']),
    dict(is_acl_redline=True, expected_doc_ids=[], forbidden_doc_ids=[]),
    dict(forbidden_keywords=['secret']), dict(forbidden_doc_ids=['x']),
    dict(expected_doc_ids=[], expected_keywords=[], reference_answer=None),
])
def test_sample_semantics(updates):
    with pytest.raises(ValidationError):
        EvalSample.model_validate(sample().model_dump() | updates)


@pytest.mark.parametrize('counts,passed', [((0,0),True), ((3,3),False), ((2,3),False)])
def test_acl_summary_validator(counts, passed):
    with pytest.raises(ValidationError):
        SuiteSummaryMetrics(total_samples=3,failed_samples=0,error_samples=0,
            acl_redline_total=counts[0],acl_redline_blocked=counts[1],acl_redline_passed=passed)
    assert SuiteSummaryMetrics(total_samples=3,failed_samples=0,error_samples=0,
        acl_redline_total=3,acl_redline_blocked=3,acl_redline_passed=True).acl_redline_passed


@pytest.mark.parametrize('passed,reasons', [(True,['failure']), (False,[])])
def test_diff_validator(passed,reasons):
    with pytest.raises(ValidationError):
        RegressionDiffReport(baseline_run_id='b',candidate_run_id='c',baseline_version='b',candidate_version='c',
            suite_comparable=True,gate_passed=passed,violations=[GateViolation(code=GateViolationCode.ACL_REDLINE_BREACHED,message='failure')],blocking_reasons=reasons)


def test_protocols(retrieval):
    assert isinstance(retrieval, RetrievalSystemPort)
    assert isinstance(FakeRetrievalSystem(), RetrievalSystemPort)
    assert isinstance(FakeAgentSystem(), AgentSystemPort)
    assert isinstance(FakeJudge(), LLMJudgePort)
