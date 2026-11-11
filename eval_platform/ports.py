"""评测中台异步端口。

三个 Protocol 均为结构化类型（runtime_checkable），被测系统无需继承即可接入：

- RetrievalSystemPort：数据中台 的 HybridRetrievalService 原样满足（search 签名完全一致），
  无需任何适配层。
- AgentSystemPort：门店、供应链与财务模块 的业务智能体 / Workflow 通过薄适配器接入端到端评测。
- LLMJudgePort：裁判模型。三个维度拆分为独立方法，便于 EvalRunner 用 asyncio.gather 并发调用，
  并允许不同维度使用不同 prompt / 模型。

适配器约束（实现方必须满足）：
1. 不得修改传入的 UserContext；被测系统必须基于它做 ACL 过滤，评测侧不做任何权限放宽。
2. 业务异常可直接抛出，由 EvalRunner 捕获并归因为 SYSTEM_ERROR，不得吞掉后返回伪造结果。
3. LLMJudgePort 返回的 score 必须落在约定区间（faithfulness/accuracy 0~1，satisfaction 1~5），
   越界视为 JUDGE_ERROR。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from data_platform.retrieval_engine.models import (
    RetrievedParentChunk,
    SearchRequest,
    SearchResponse,
    UserContext,
)
from eval_platform.models import (
    AgentInvocation,
    AgentResponse,
    EvalScenario,
    JudgeScore,
)


@runtime_checkable
class RetrievalSystemPort(Protocol):
    """检索系统端口（被测对象：数据中台 HybridRetrievalService）。"""

    async def search(self, request: SearchRequest) -> SearchResponse:
        """执行一次带 ACL 前置过滤的检索。

        - 降级时返回 degraded=True 的正常响应；不可用时抛异常（由 EvalRunner 归因 SYSTEM_ERROR）。
        """
        ...


@runtime_checkable
class AgentSystemPort(Protocol):
    """业务智能体端口（被测对象：门店、供应链与财务模块 门店 SOP / 供应链 / 财务预审智能体及 Workflow）。"""

    async def invoke(self, invocation: AgentInvocation) -> AgentResponse:
        """以 invocation.user_context 身份执行一次端到端问答。

        - retrieved_contexts 必须为智能体实际送入生成阶段的上下文（按排名），
          EvalRunner 据此计算检索指标、越权检测与忠实度。
        - latency_ms 由被测系统自报（不含裁判耗时）；EvalRunner 另行计时作交叉校验。
        """
        ...


@runtime_checkable
class LLMJudgePort(Protocol):
    """裁判模型端口。"""

    async def score_faithfulness(
        self,
        *,
        question: str,
        answer: str,
        contexts: list[RetrievedParentChunk],
    ) -> JudgeScore:
        """忠实度（0~1）：回答中的事实陈述有多大比例能被 contexts 支撑。

        contexts 为空且回答含事实陈述时应给低分；拒答类回答给 1.0。
        """
        ...

    async def score_accuracy(
        self,
        *,
        question: str,
        answer: str,
        reference_answer: str | None,
        expected_keywords: list[str],
    ) -> JudgeScore:
        """准确率（0~1）：回答与参考答案 / 关键事实的语义一致程度。

        reference_answer 为 None 时仅依据 expected_keywords 判断。
        """
        ...

    async def simulate_satisfaction(
        self,
        *,
        question: str,
        answer: str,
        scenario: EvalScenario,
        user_context: UserContext,
    ) -> JudgeScore:
        """模拟用户满意度（1~5 Likert）：以该场景下 user_context.roles 所示岗位（如店员、店长、财务）
        的视角，评价回答的可执行性、简洁度与语气。"""
        ...
