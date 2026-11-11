import asyncio
from time import perf_counter
import pytest
from eval_platform.models import EvalRunConfig, AgentResponse, TokenUsage, FailureCategory as F
from eval_platform.runner import load_eval_suite, compute_suite_fingerprint, EvalRunner
from data_platform.retrieval_engine.models import SearchResponse
from tests.eval_platform.conftest import sample, chunk
from tests.eval_platform.fakes import FakeRetrievalSystem, FakeAgentSystem, FakeJudge


async def test_load_jsonl_errors_and_duplicates(tmp_path):
    path=tmp_path/'samples.jsonl'
    path.write_text(sample().model_dump_json()+'\n\n'+sample('s2').model_dump_json())
    assert len(await load_eval_suite(path))==2
    path.write_text(sample().model_dump_json()+'\n\n{"sample_id":"bad"}')
    with pytest.raises(ValueError,match='3.*bad'): await load_eval_suite(path)
    path.write_text((sample().model_dump_json()+'\n')*2)
    with pytest.raises(ValueError,match='duplicate'): await load_eval_suite(path)


def test_fingerprint():
    samples=[sample(),sample('s2')]
    assert compute_suite_fingerprint(samples)==compute_suite_fingerprint(samples[::-1])
    assert compute_suite_fingerprint(samples)!=compute_suite_fingerprint([sample(question='changed'),sample('s2')])


def test_constructor_validation(config):
    with pytest.raises(ValueError): EvalRunner(config=config)
    with pytest.raises(ValueError): EvalRunner(config=config.model_copy(update={'mode':'end_to_end'}),agent=FakeAgentSystem())


async def test_concurrency(config):
    system=FakeRetrievalSystem(delay=.1)
    runner=EvalRunner(config=config.model_copy(update={'max_concurrency':4}),retrieval=system)
    start=perf_counter()
    run=await runner.run([sample(f's{i}') for i in range(20)],suite_name='parallel')
    elapsed=perf_counter()-start
    assert system.max_inflight==4 and system.inflight==0
    assert .45 <= elapsed < .8
    assert run.started_at.utcoffset().total_seconds()==0 and run.finished_at>=run.started_at


async def test_order_and_request_passthrough(config):
    samples=[sample('slow'),sample('fast')]
    system=FakeRetrievalSystem(delays={'slow':.03})
    run=await EvalRunner(config=config.model_copy(update={'top_k':2,'enable_rerank':False}),retrieval=system).run(samples,suite_name='ordered')
    assert [r.sample_id for r in run.results]==['slow','fast']
    assert system.calls[0].user_context==samples[0].user_context
    assert system.calls[0].query=='slow' and system.calls[0].top_k==2 and not system.calls[0].enable_rerank


async def test_timeout_and_errors_isolated(config):
    system=FakeRetrievalSystem(responses={'boom':RuntimeError('broken'),'ok':SearchResponse(query='ok',results=[chunk()],latency_ms=2)},delays={'slow':.1})
    run=await EvalRunner(config=config.model_copy(update={'sample_timeout_s':.03}),retrieval=system).run(
        [sample('slow'),sample('boom'),sample('ok'),sample('red',question='boom',is_acl_redline=True)],suite_name='errors')
    assert [r.failure_category for r in run.results]==[F.TIMEOUT,F.SYSTEM_ERROR,F.NONE,F.SYSTEM_ERROR]
    assert run.summary.retrieval_hit_rate==pytest.approx(1/3)
    assert run.summary.acl_redline_blocked==0
    for r in run.results[:2]:
        assert r.hit_at_k==r.recall_at_k==r.mrr==0 and r.latency_ms is None
        assert 'Traceback' not in r.error_message


def agent_runner(config,judge,agent=None):
    return EvalRunner(config=config.model_copy(update={'mode':'end_to_end'}),agent=agent or FakeAgentSystem(
        {'s1':AgentResponse(answer='标准事实',retrieved_contexts=[chunk()],latency_ms=1,token_usage=TokenUsage(prompt_tokens=10,completion_tokens=4))}),judge=judge)


@pytest.mark.parametrize('judge',[
    FakeJudge(errors={'accuracy':RuntimeError('quota')}), FakeJudge(scores={'accuracy':1.1}),
    FakeJudge(scores={'accuracy':float('nan')}), FakeJudge(delays={'accuracy':.1}),
])
async def test_judge_partial_failures(config,judge):
    run=await agent_runner(config.model_copy(update={'judge_timeout_s':.03}),judge).run([sample()],suite_name='judge')
    r=run.results[0]
    assert r.failure_category==F.JUDGE_ERROR and r.answer_accuracy_score is None
    assert r.faithfulness_score==1 and r.satisfaction_score==5 and r.hit_at_k==1
    assert r.token_cost==14 and r.judge_token_cost in (6,9)


async def test_judges_parallel_and_cost_separation(config):
    judge=FakeJudge(delay=.2)
    start=perf_counter()
    run=await agent_runner(config,judge).run([sample()],suite_name='judge')
    elapsed=perf_counter()-start
    assert .19 <= elapsed < .35
    assert run.results[0].latency_ms < 100
    assert run.summary.total_token_cost==14 and run.summary.total_judge_token_cost==9
    assert run.results[0].satisfaction_score==5


async def test_redline_calls_agent_but_skips_judge_even_refused(config):
    judge=FakeJudge()
    agent=FakeAgentSystem({'s1':AgentResponse(answer='拒答，但账户是 SECRET',refused=True,latency_ms=1)})
    run=await agent_runner(config,judge,agent).run([sample(is_acl_redline=True,forbidden_keywords=['secret'])],suite_name='red')
    assert len(agent.calls)==1 and not judge.calls
    assert run.results[0].failure_category==F.ACL_LEAK
    assert run.results[0].satisfaction_score is None and run.results[0].keyword_coverage is None
    assert agent.calls[0].trace_id==f'{run.run_id}:s1'


async def test_cancel_propagates(config):
    system=FakeRetrievalSystem(delay=10)
    task=asyncio.create_task(EvalRunner(config=config,retrieval=system).run([sample()],suite_name='cancel'))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert system.inflight==0


async def test_cancel_from_judge_propagates(config):
    runner=agent_runner(config,FakeJudge(errors={'accuracy':asyncio.CancelledError()}))
    with pytest.raises(asyncio.CancelledError): await runner.run([sample()],suite_name='cancel')


async def test_empty_and_duplicate_rejected(config):
    runner=EvalRunner(config=config,retrieval=FakeRetrievalSystem())
    for samples in ([],[sample(),sample()]):
        with pytest.raises(ValueError): await runner.run(samples,suite_name='invalid')


async def test_unavailable_accuracy_is_none(config):
    judge=FakeJudge()
    run=await agent_runner(config,judge).run([sample(expected_keywords=[],reference_answer=None)],suite_name='na')
    assert run.results[0].answer_accuracy_score is None and judge.calls['accuracy']==0


@pytest.mark.parametrize('name,value', [('faithfulness',-1),('faithfulness',float('inf')),('satisfaction',0),('satisfaction',6)])
async def test_each_judge_range_is_enforced(config,name,value):
    run=await agent_runner(config,FakeJudge(scores={name:value})).run([sample()],suite_name='invalid-score')
    r=run.results[0]
    assert r.failure_category==F.JUDGE_ERROR and r.judge_token_cost==9
    assert getattr(r,{'faithfulness':'faithfulness_score','satisfaction':'satisfaction_score'}[name]) is None


async def test_whole_sample_timeout_includes_judge(config):
    runner=agent_runner(config.model_copy(update={'sample_timeout_s':.02}),FakeJudge(delay=.1))
    run=await runner.run([sample()],suite_name='total-timeout')
    assert run.results[0].failure_category==F.TIMEOUT and run.results[0].hit_at_k==0
    assert run.results[0].latency_ms is None


async def test_cancel_during_judging(config):
    judge=FakeJudge(delay=10)
    task=asyncio.create_task(agent_runner(config,judge).run([sample()],suite_name='cancel-judge'))
    while not judge.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
