"""Controllable async test ports with observable concurrency and fault injection."""
import asyncio
from collections import Counter
from data_platform.retrieval_engine.models import SearchResponse
from eval_platform.models import AgentResponse, JudgeScore, TokenUsage


class FakeRetrievalSystem:
    def __init__(self, responses=None, *, delay=0, delays=None):
        self.responses = responses or {}
        self.delay, self.delays = delay, delays or {}
        self.calls = []
        self.inflight = self.max_inflight = 0

    async def _respond(self, question, default):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delays.get(question, self.delay))
            response = self.responses.get(question, default)
            if isinstance(response, BaseException):
                raise response
            return response
        finally:
            self.inflight -= 1

    async def search(self, request):
        self.calls.append(request)
        return await self._respond(request.query, SearchResponse(query=request.query, latency_ms=1))


class FakeAgentSystem(FakeRetrievalSystem):
    async def invoke(self, invocation):
        self.calls.append(invocation)
        return await self._respond(invocation.question, AgentResponse(answer="标准事实", latency_ms=1))


class FakeJudge:
    def __init__(self, *, scores=None, errors=None, delays=None, delay=0, tokens=3):
        self.scores = {"faithfulness": 1, "accuracy": 1, "satisfaction": 5} | (scores or {})
        self.errors, self.delays, self.delay = errors or {}, delays or {}, delay
        self.tokens, self.calls = tokens, Counter()

    async def _score(self, name):
        self.calls[name] += 1
        await asyncio.sleep(self.delays.get(name, self.delay))
        if name in self.errors:
            raise self.errors[name]
        return JudgeScore(score=self.scores[name], reason=name, token_usage=TokenUsage(prompt_tokens=self.tokens))

    async def score_faithfulness(self, *, question, answer, contexts):
        return await self._score("faithfulness")

    async def score_accuracy(self, *, question, answer, reference_answer, expected_keywords):
        return await self._score("accuracy")

    async def simulate_satisfaction(self, *, question, answer, scenario, user_context):
        return await self._score("satisfaction")
