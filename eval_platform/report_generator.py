"""自动化运营评估报告生成器。

输出面向运营 / 业务负责人的 Markdown 报告，章节固定顺序：
    1. 标题与运行信息（suite、版本、模式、样本数、时间、门禁结论徽标）
    2. 核心指标看板（检索命中率 / 平均召回率 / MRR / 回答准确率 / 忠实度 / 满意度 /
       P50 / P95 / 降级率 / 错误率 / 越权拦截率 / token 成本；有基线时附带涨跌箭头）
    3. 上线门禁结论（仅 diff 不为 None 时；逐条列出阻断原因）
    4. 分场景表现（电商运营SOP / 供应链 / 财务审批）
    5. 分部门表现（按 dept_id）
    6. 坏例归因分布（FailureCategory 计数与占比，降序）
    7. Top Bad Cases
    8. 优化建议（规则驱动，见 RECOMMENDATION_RULES）

渲染为纯函数（无 I/O），仅 write 方法落盘。同一输入必须产生字节级一致的输出（可做 snapshot 测试）。
"""

from __future__ import annotations

from pathlib import Path
import asyncio
import html

from eval_platform.runner import RegressionGate
from eval_platform.models import GateConfig, GateViolationCode

from eval_platform.models import (
    EvalRunRecord,
    FailureCategory,
    RegressionDiffReport,
    SliceMetrics,
    SuiteSummaryMetrics,
)

RECOMMENDATION_RULES: dict[FailureCategory, str] = {
    FailureCategory.ACL_LEAK: "【P0 红线】存在越权泄露：立即阻断发布，排查 RetrievalFilter 下推与 _enforce_filter 纵深校验，核对 kb_documents.allowed_roles 配置。",
    FailureCategory.SYSTEM_ERROR: "被测系统异常：检查 Milvus / ES / 台账连通性与 ToolErrorResponse 映射，确认无裸 500。",
    FailureCategory.TIMEOUT: "响应超时：评估 branch_timeout_s / rerank_timeout_s 配置，或降低 rerank_candidates。",
    FailureCategory.RETRIEVAL_MISS: "期望文档未召回：检查父子块切分粒度、BM25 分词与同义词词典，扩大 vector_top_k / keyword_top_k。",
    FailureCategory.RANKING_LOW: "召回命中但排序靠后：启用或更换精排模型，调整 rrf_k 与 rerank_candidates。",
    FailureCategory.HALLUCINATION: "回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。",
    FailureCategory.ANSWER_INCORRECT: "回答与标准答案不一致：复核手册版本是否已同步发布，检查表格类父块是否被截断。",
    FailureCategory.INCOMPLETE_ANSWER: "关键事实遗漏：检查表格行是否完整保留在父块内，提示模型完整列出配比 / 审批角色等要素。",
    FailureCategory.LOW_SATISFACTION: "模拟满意度偏低：精简回答结构，优先给出一线可直接执行的步骤。",
    FailureCategory.DEGRADED_SERVICE: "存在降级请求：排查 degraded_reasons 高频原因码对应的下游依赖。",
    FailureCategory.JUDGE_ERROR: "裁判模型调用失败：检查裁判服务配额与超时，失败样本的端到端指标未纳入统计。",
}
"""失败归因 -> 优化建议文案。仅输出在本轮中出现次数 > 0 的类别，按出现次数降序。"""


class MarkdownReportGenerator:
    """运营评估报告生成器。"""

    def __init__(self, *, top_bad_cases: int = 10, decimals: int = 1) -> None:
        """top_bad_cases：无 diff 时从 run.results 自选坏例的条数；decimals：百分比小数位。"""
        if top_bad_cases < 0 or not 0 <= decimals <= 10:
            raise ValueError("invalid report formatting parameters")
        self.top_bad_cases, self.decimals = top_bad_cases, decimals

    def render(self, run: EvalRunRecord, diff: RegressionDiffReport | None = None) -> str:
        """按模块文档章节顺序拼接完整报告；diff 不为 None 时要求 diff.candidate_run_id == run.run_id，否则 ValueError。"""
        if diff is not None and diff.candidate_run_id != run.run_id:
            raise ValueError("diff candidate_run_id does not match run")
        sections = [self._render_header(run, diff), self._render_kpi_dashboard(run.summary, diff)]
        if diff is not None:
            sections.append(self._render_gate_section(diff))
        sections.extend([
            self._render_slice_table("分场景表现", run.summary.by_scenario),
            self._render_slice_table("分部门表现", run.summary.by_dept),
            self._render_failure_distribution(run.summary), self._render_bad_cases(run, diff),
            "## 优化建议\n\n" + "\n".join(f"- {_escape(item)}" for item in self.build_recommendations(run.summary, diff)),
        ])
        return "\n\n".join(sections) + "\n"

    async def write(self, path: Path, content: str) -> None:
        """经 asyncio.to_thread 以 UTF-8 写入；父目录不存在时创建。"""
        def save():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        await asyncio.to_thread(save)

    def _render_header(self, run: EvalRunRecord, diff: RegressionDiffReport | None) -> str:
        """标题、运行元数据与门禁徽标（✅ 通过 / ❌ 阻断 / — 无基线）。"""
        badge = "— 无基线" if diff is None else ("✅ 通过" if diff.gate_passed else "❌ 阻断")
        return (f"# 智能体运营评估报告\n\n"
                f"- 评测集：{_escape(run.suite_name)}\n"
                f"- 运行 ID：{_escape(run.run_id)}\n"
                f"- 系统 / 版本：{_escape(run.config.system_name)} / {_escape(run.config.system_version)}\n"
                f"- 模式：{run.config.mode.value}；Top K：{run.config.top_k}；精排：{run.config.enable_rerank}\n"
                f"- 样本数：{run.summary.total_samples}；指纹：{run.suite_fingerprint}\n"
                f"- UTC 时间：{run.started_at.isoformat()} → {run.finished_at.isoformat()}\n"
                f"- 门禁：{badge}")

    def _render_kpi_dashboard(self, summary: SuiteSummaryMetrics, diff: RegressionDiffReport | None) -> str:
        """核心指标看板表：指标 | 当前值 | 基线 | 变化 | 状态。None 显示为 "N/A"；
        越权拦截率未达 100% 时状态列必须显示红线告警。"""
        deltas = {d.metric: d for d in diff.metric_deltas} if diff else {}
        rows = []
        for metric, label in _KPI_LABELS.items():
            value = getattr(summary, metric)
            delta = deltas.get(metric)
            baseline = self._format(metric, delta.baseline) if delta else "—"
            change, state = "—", "—"
            if delta is not None and delta.abs_delta is not None:
                arrow = "↑" if delta.abs_delta > 0 else "↓" if delta.abs_delta < 0 else "→"
                change = f"{arrow} {self._format(metric, abs(delta.abs_delta))}"
                state = "回退超限" if delta.regressed else "正常"
            if metric == "acl_block_rate" and not summary.acl_redline_passed:
                state = "🚨 红线告警"
            rows.append([label, self._format(metric, value), baseline, change, state])
        return "## 核心指标看板\n\n" + _table(["指标", "当前值", "基线", "变化", "状态"], rows)

    def _render_gate_section(self, diff: RegressionDiffReport) -> str:
        """门禁结论与阻断原因列表，以及新增失败 / 修复样本数。"""
        conclusion = "✅ 通过" if diff.gate_passed else "❌ 阻断"
        reasons = "\n".join(f"- [{v.code.value}] {_escape(v.message)}" for v in diff.violations)
        return (f"## 上线门禁结论\n\n{conclusion}；样本集可比：{diff.suite_comparable}\n\n"
                f"基线版本：{_escape(diff.baseline_version)} → 候选版本：{_escape(diff.candidate_version)}\n\n"
                f"新增失败：{len(diff.newly_failed_sample_ids)}；修复：{len(diff.fixed_sample_ids)}\n\n"
                + (reasons or "全部门禁检查通过。"))

    def _render_slice_table(self, title: str, slices: list[SliceMetrics]) -> str:
        """分场景 / 分部门通用表格：切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截。"""
        rows = [[s.slice_label, s.total_samples, s.failed_samples,
                 self._format("retrieval_hit_rate", s.retrieval_hit_rate),
                 self._format("mean_recall_at_k", s.mean_recall_at_k),
                 self._format("answer_accuracy_rate", s.answer_accuracy_rate),
                 self._format("mean_satisfaction", s.mean_satisfaction),
                 self._format("latency_p95_ms", s.latency_p95_ms),
                 f"{s.acl_redline_blocked}/{s.acl_redline_total}" if s.acl_redline_total else "N/A"] for s in slices]
        return f"## {title}\n\n" + _table(["切片", "样本数", "失败数", "命中率", "召回率", "准确率", "满意度", "P95", "红线拦截"], rows)

    def _render_failure_distribution(self, summary: SuiteSummaryMetrics) -> str:
        """归因分布表：归因 | 数量 | 占比（不含 NONE），按数量降序、枚举值升序。"""
        rows = [[category.value, count, self._format("rate", count / summary.total_samples if summary.total_samples else None)]
                for category, count in sorted(summary.failure_distribution.items(), key=lambda pair: (-pair[1], pair[0].value))
                if category != FailureCategory.NONE and count > 0]
        return "## 坏例归因分布\n\n" + (_table(["归因", "数量", "占比"], rows) if rows else "无坏例。")

    def _render_bad_cases(self, run: EvalRunRecord, diff: RegressionDiffReport | None) -> str:
        """Top Bad Cases：有 diff 用 diff.top_bad_cases，否则从 run.results 按同一 severity 规则自选。
        question / detail 中的 "|" 与换行必须转义，避免破坏表格。"""
        cases = diff.top_bad_cases if diff is not None else RegressionGate(
            GateConfig(top_bad_cases=min(self.top_bad_cases, 100)))._select_bad_cases(run, run, [])
        rows = [[c.sample_id, c.scenario.label, c.question, c.failure_category.value,
                 "是" if c.newly_failed else "否", c.severity, c.detail] for c in cases]
        return "## Top Bad Cases\n\n" + (_table(["样本", "场景", "问题", "归因", "新增失败", "严重度", "详情"], rows) if rows else "无坏例。")

    def build_recommendations(self, summary: SuiteSummaryMetrics, diff: RegressionDiffReport | None = None) -> list[str]:
        """规则驱动的优化建议列表。

        - 按 failure_distribution 出现次数降序映射 RECOMMENDATION_RULES；ACL_LEAK 始终置顶。
        - 有 diff 时，对每条 METRIC_REGRESSION / LATENCY_REGRESSION 追加一条针对性建议。
        - 无任何问题时返回 ["本轮评测未发现需优化项，建议扩充长尾样本。"]。
        """
        ordered = sorted(summary.failure_distribution.items(),
            key=lambda pair: (pair[0] != FailureCategory.ACL_LEAK, -pair[1], pair[0].value))
        recommendations = [RECOMMENDATION_RULES[c] for c, count in ordered if count > 0 and c in RECOMMENDATION_RULES]
        if diff is not None:
            for v in diff.violations:
                if v.code in (GateViolationCode.METRIC_REGRESSION, GateViolationCode.LATENCY_REGRESSION):
                    recommendations.append(f"针对 {v.metric or '新增失败样本'} 复核基线配置并定位回退原因：{v.message}")
        if not summary.acl_redline_passed and not summary.failure_distribution.get(FailureCategory.ACL_LEAK):
            recommendations.insert(0, "【P0 红线】补齐红线样本并完成验证；未验证或缺失红线覆盖时禁止发布。")
        return recommendations or ["本轮评测未发现需优化项，建议扩充长尾样本。"]


    def _format(self, metric: str, value: float | None) -> str:
        if value is None:
            return "N/A"
        if "token" in metric:
            return str(int(value))
        if "latency" in metric:
            return f"{value:.{self.decimals}f} ms"
        if metric == "mean_satisfaction":
            return f"{value:.{self.decimals}f} 分"
        if metric == "mean_mrr":
            return f"{value:.{max(3, self.decimals)}f}"
        return f"{value:.{self.decimals}%}"


def _escape(value) -> str:
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _table(headers, rows) -> str:
    return "\n".join("| " + " | ".join(_escape(v) for v in row) + " |" for row in
                     [headers, ["---"] * len(headers), *rows])


_KPI_LABELS = {
    "retrieval_hit_rate": "检索命中率", "mean_recall_at_k": "平均召回率", "mean_mrr": "MRR",
    "answer_accuracy_rate": "回答准确率", "mean_answer_accuracy": "平均回答准确度",
    "mean_faithfulness": "忠实度", "mean_keyword_coverage": "关键词覆盖率", "mean_satisfaction": "满意度（1–5）",
    "latency_p50_ms": "P50", "latency_p95_ms": "P95", "degraded_rate": "降级率", "error_rate": "错误率",
    "acl_block_rate": "越权拦截率", "total_token_cost": "被测系统 Token", "total_judge_token_cost": "裁判 Token",
}
