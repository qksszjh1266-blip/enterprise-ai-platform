"""Offline, deterministic fixtures; rule scores are not human or LLM evaluations."""
from collections.abc import Sequence
import re
from time import perf_counter

from data_platform.retrieval_engine.models import RetrievedParentChunk, SearchRequest, UserContext
from eval_platform.evaluators import RetrievalMetricsCalculator, _normalize
from eval_platform.models import AgentInvocation, AgentResponse, EvalScenario, JudgeScore
from eval_platform.ports import RetrievalSystemPort

REFUSAL = "未找到有权限访问的依据，请联系所属部门负责人确认。"


class RuleBasedAgent:
    """Extract accessible passages without reading evaluation labels or references."""

    def __init__(self, retrieval: RetrievalSystemPort, *, top_k: int = 5, enable_rerank: bool = True):
        self.retrieval, self.top_k, self.enable_rerank = retrieval, top_k, enable_rerank

    async def invoke(self, invocation: AgentInvocation) -> AgentResponse:
        start = perf_counter()
        response = await self.retrieval.search(SearchRequest(query=invocation.question,
            user_context=invocation.user_context, top_k=self.top_k, enable_rerank=self.enable_rerank))
        answer = "\n".join(p.full_content for p in response.results) or REFUSAL
        return AgentResponse(answer=answer, retrieved_contexts=response.results,
            cited_doc_ids=list(dict.fromkeys(p.doc_id for p in response.results)),
            refused=not response.results, latency_ms=(perf_counter() - start) * 1000,
            degraded=response.degraded, degraded_reasons=response.degraded_reasons)


class RuleBasedJudge:
    """Lexical evidence checks and an explicit 1–5 usability rubric; zero model tokens."""

    async def score_faithfulness(self, *, question: str, answer: str,
                                 contexts: list[RetrievedParentChunk]) -> JudgeScore:
        if answer == REFUSAL:
            return JudgeScore(score=1, reason="规则：明确拒答，不添加事实")
        claims = [_normalize(c) for c in re.split(r"[\n。！？；]", answer) if _normalize(c)]
        evidence = _normalize("\n".join(c.full_content for c in contexts))
        value = sum(c in evidence for c in claims) / len(claims) if claims else 0
        return JudgeScore(score=value, reason="规则：逐句检查是否存在于可见上下文，非语义裁判")

    async def score_accuracy(self, *, question: str, answer: str, reference_answer: str | None,
                             expected_keywords: list[str]) -> JudgeScore:
        keywords = expected_keywords or ([reference_answer] if reference_answer else [])
        score = RetrievalMetricsCalculator.keyword_coverage(answer, keywords)
        if score is None:
            raise ValueError("accuracy requires reference facts")
        return JudgeScore(score=score, reason="规则：标准事实关键词覆盖率")

    async def simulate_satisfaction(self, *, question: str, answer: str, scenario: EvalScenario,
                                    user_context: UserContext) -> JudgeScore:
        if not answer.strip() or answer == REFUSAL:
            score = 1
        elif len(answer) > 1000:
            score = 2
        elif len(answer) > 500:
            score = 3
        elif any(word in answer for word in ("检查", "审批", "记录", "隔离", "确认")):
            score = 5
        else:
            score = 4
        return JudgeScore(score=score, reason="规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度")
