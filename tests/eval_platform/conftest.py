from datetime import datetime, timezone
import pytest
from data_platform.retrieval_engine.models import RetrievedParentChunk, RetrievalBranch, UserContext
from eval_platform.models import EvalSample, EvalRunConfig, SampleEvalResult, EvalRunRecord
from eval_platform.evaluators import SuiteMetricsAggregator


def chunk(doc="d1", parent="p1", content="标准事实"):
    return RetrievedParentChunk(parent_id=parent, doc_id=doc, version=1, full_content=content,
        score=1, rrf_score=1, source_branches=[RetrievalBranch.KEYWORD])


def sample(sid="s1", **kwargs):
    values = dict(sample_id=sid, scenario="store_sop", question=sid,
        user_context=UserContext(user_id="u", dept_id="store_ops", roles=["clerk"]), expected_doc_ids=["d1"],
        expected_keywords=["标准事实"])
    if kwargs.get("is_acl_redline"):
        values.update(expected_doc_ids=[], expected_keywords=[], forbidden_doc_ids=["secret"])
    return EvalSample(**(values | kwargs))


def result(sid="s1", **kwargs):
    return SampleEvalResult(**(dict(sample_id=sid, scenario="store_sop", dept_id="store_ops", question=sid,
        is_acl_redline=False, hit_at_k=1., recall_at_k=1., mrr=1., latency_ms=10.) | kwargs))


def record(results=None, **summary_updates):
    results = results if results is not None else [result(), result("acl", is_acl_redline=True, hit_at_k=None, recall_at_k=None, mrr=None)]
    config = EvalRunConfig(system_name="test", system_version="v1")
    summary = SuiteMetricsAggregator(config).aggregate(results)
    summary = type(summary).model_validate(summary.model_dump() | summary_updates)
    return EvalRunRecord(run_id="test", suite_name="test", suite_fingerprint="same", config=config,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        results=results, summary=summary)


@pytest.fixture
def config():
    return EvalRunConfig(system_name="test", system_version="v1")
