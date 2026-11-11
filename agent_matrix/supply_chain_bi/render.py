"""确定性话术与结果渲染。

除 SqlGeneratorModelPort.summarize_analysis 外，所有面向用户的文本（追问 / 拒答 / 重试耗尽 / 数据源不可用 /
模板结论 / 预警说明 / 范围说明）都由本模块确定性生成，不调用模型。

评测上下文格式 BI_RESULT_V1（agent.invoke 把结果表渲染为 RetrievedParentChunk.full_content，
eval_judge.DataAnalysisJudge 按同一格式解析；两侧只能通过本模块的 render / parse 函数读写）：

    [BI_RESULT_V1]
    catalog_version: 1
    metric_ids: material_loss_rate
    as_of_date: 2026-11-01
    scope: 华东大区 · 授权仓 2 个
    sql: SELECT ... LIMIT 200
    columns: warehouse_id | material_id | loss_rate
    row: WH_E01 | MAT_PROBIOTIC | 0.0523
    row: ...
    truncated: false

    [BI_ALERTS_V1]
    alert: safety_stock_breach | p1 | WH_E01 | MAT_PROBIOTIC | current=12.0 | threshold=40.0 | gap=28.0 | replenish | qty=28.0

- 单元格分隔符 ` | `；单元格内的 `|` 与换行转义为 `\\|` / `\\n`；None 渲染为 `NULL`；float 用 repr（保留原始精度）。
- 仅渲染 ResultTable.rows（展示行），不渲染敏感信息；范围只渲染 scope_summary，不渲染仓库 ID 列表。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from agent_matrix.supply_chain_bi.models import ScopeLevel, MetricUnit

from collections.abc import Sequence
from datetime import date

from agent_matrix.supply_chain_bi.models import (
    AlertRuleConfig,
    AnalysisNarrativeDraft,
    InventoryAlert,
    MetricDefinition,
    RefusalReason,
    ResultTable,
    ScalarValue,
    SqlAttempt,
    SqlExecutionResult,
    WarehouseScopeFilter,
)

RENDER_VERSION = "supply_chain_bi.v1"
"""修改任何话术常量必须递增。"""

RESULT_CONTEXT_HEADER = "[BI_RESULT_V1]"
ALERT_CONTEXT_HEADER = "[BI_ALERTS_V1]"

REFUSAL_TEMPLATES: dict[RefusalReason, str] = {
    RefusalReason.OUT_OF_SCOPE: "我是供应链经营分析助手，只能回答库存、效期、损耗、店铺销量等供应链数据问题。",
    RefusalReason.SENSITIVE_FIELD: "采购底价、供应商合同价及成本类金额属于受控数据，本助手无法查询，请通过采购部审批流程获取。",
    RefusalReason.SCOPE_VIOLATION: "您当前的数据权限不包含所询问的大区或仓库，以下分析只能在您的授权范围内进行。如需扩大范围请联系供应链数据管理员。",
    RefusalReason.POLICY_VIOLATION: "该问题生成的查询触发了数据安全策略，已终止执行。请换一种问法，只询问库存、效期、损耗或销量指标。",
}
"""拒答话术：不得包含任何表名、列名、仓库 ID 或"该数据存在"的确认。"""

SQL_RETRY_EXHAUSTED_NOTICE = "多次尝试后仍未能生成符合口径与安全规范的查询，请把问题拆小（例如指定商品、仓库或日期范围）后重试。"
DATA_SOURCE_UNAVAILABLE_NOTICE = "分析数据源暂不可用，本次未执行任何查询。紧急补货请按线下预警流程处理，稍后可重试。"
TRUNCATION_NOTICE = "（结果超过 {max_rows} 行已截断，结论仅基于前 {max_rows} 行，请增加筛选条件获取完整口径。）"
EMPTY_RESULT_NOTICE = "在 {scope_summary} 范围内、基准日 {as_of_date} 未查询到符合条件的数据。"


def render_scope_summary(scope: WarehouseScopeFilter, *, region_name: str | None = None) -> str:
    """"华东大区 · 授权仓 2 个" / "华东大区（全部仓库）" / "全国"；不列举仓库 ID。"""
    if scope.scope_level is ScopeLevel.NATIONAL:
        return "全国"
    region = region_name or {"R_EAST": "华东大区", "R_SOUTH": "华南大区"}.get(scope.region_id, "授权大区")
    return f"{region} · 授权仓 {len(scope.authorized_warehouse_ids)} 个" if scope.scope_level is ScopeLevel.WAREHOUSE else f"{region}（全部仓库）"


def render_refusal(reason: RefusalReason) -> str:
    """返回 REFUSAL_TEMPLATES[reason]。"""
    return REFUSAL_TEMPLATES[reason]


def render_clarification(candidates: Sequence[MetricDefinition], model_question: str | None) -> str:
    """候选指标 ≥ 2 时列出"您想看的是：A（口径一句话）还是 B …"（最多 3 个）；否则使用 model_question；
    二者皆无时用固定兜底问句。"""
    if len(candidates) >= 2:
        return "您想看的是：" + "还是".join(f"{m.display_name}（{m.calculation_caliber.split('。')[0]}）" for m in candidates[:3]) + "？"
    return model_question or "请说明要分析的库存、效期、损耗或销量指标，以及商品和日期范围。"


def render_result_table(execution: SqlExecutionResult, *, display_rows: int) -> ResultTable:
    """截取前 display_rows 行构造 ResultTable；source_truncated = execution.truncated。"""
    return ResultTable(columns=execution.columns, rows=execution.rows[:display_rows], total_row_count=execution.row_count, displayed_row_count=min(display_rows, execution.row_count), source_truncated=execution.truncated)


def render_template_conclusion(
    *,
    metrics: Sequence[MetricDefinition],
    table: ResultTable,
    alerts: Sequence[InventoryAlert],
    scope_summary: str,
    as_of_date: date,
    max_rows: int,
) -> str:
    """数值核验失败或摘要模型失败时的确定性结论：

    1. 首句 "基准日 {as_of_date}，{scope_summary} 范围内…"；
    2. 列出前 5 行中首个指标列的取值（按 metric.unit / decimal_places 格式化，RATIO 显示为百分比）；
    3. 预警计数（按 severity 汇总）；
    4. 截断时追加 TRUNCATION_NOTICE；空结果用 EMPTY_RESULT_NOTICE。
    """
    if not table.rows:
        return EMPTY_RESULT_NOTICE.format(scope_summary=scope_summary, as_of_date=as_of_date)
    parts = [f"基准日 {as_of_date}，{scope_summary} 范围内查询结果如下。"]
    metric = metrics[0] if metrics else None
    names = [c.name for c in table.columns]
    index = next((i for i,n in enumerate(names) if metric and _metric_column(n, metric.metric_id)), None)
    if index is None:
        index = next((i for i in range(len(names)) if any(isinstance(r[i], (int,float)) and not isinstance(r[i],bool) for r in table.rows)), None)
    for row in table.rows[:5]:
        label = " · ".join(str(v) for v in row if isinstance(v,str))
        if index is not None:
            value = format_metric_value(row[index], metric) if metric else str(row[index])
            parts.append(f"{label}：{metric.display_name if metric else names[index]} {value}。")
        elif label:
            parts.append(label + "。")
    if alerts:
        counts = Counter(a.severity.value.upper() for a in alerts)
        parts.append("库存预警：" + "，".join(f"{level} {count} 项" for level,count in sorted(counts.items())) + "；请按预警补货、调拨或报损。")
    return compose_final_answer(conclusion="\n".join(parts), draft=None, truncated=table.source_truncated, max_rows=max_rows)


def render_alert_rationale(alert_fields: dict[str, ScalarValue], rules: AlertRuleConfig) -> str:
    """预警 rationale 模板，如 "可用库存 12kg，低于安全库存 40kg，按近 7 日日均出库预计 0.8 天断货"。"""
    f = alert_fields
    if f.get("alert_type") == "safety_stock_breach":
        return f"可用库存 {f['current_value']:g}{f['quantity_unit']}，低于安全库存 {f['threshold_value']:g}{f['quantity_unit']}，缺口 {f['gap_qty']:g}{f['quantity_unit']}。"
    return f"批次剩余效期 {f['days_to_expiry']} 天，积压量 {f['gap_qty']:g}{f['quantity_unit']}，请优先消化或按流程处置。"


def compose_final_answer(*, conclusion: str, draft: AnalysisNarrativeDraft | None, truncated: bool, max_rows: int) -> str:
    """最终 conclusion 文本：结论正文 + （follow_up_suggestions，最多 3 条）+ （截断提示）。"""
    parts = [conclusion]
    if draft and draft.follow_up_suggestions:
        parts.append("建议：" + "；".join(draft.follow_up_suggestions[:3]))
    if truncated:
        notice = TRUNCATION_NOTICE.format(max_rows=max_rows)
        if notice not in conclusion: parts.append(notice)
    return "\n".join(parts)


def format_metric_value(value: float | None, metric: MetricDefinition) -> str:
    """按 unit / decimal_places 格式化；None → "—"；RATIO → 百分比（decimal_places - 2 位小数，至少 0 位）。"""
    if value is None:
        return "—"
    if metric.unit is MetricUnit.RATIO:
        return f"{value * 100:.{max(0, metric.decimal_places - 2)}f}%"
    return f"{value:.{metric.decimal_places}f}"


# ---------------------------------------------------------------------------
# 评测上下文（BI_RESULT_V1 / BI_ALERTS_V1）
# ---------------------------------------------------------------------------


def render_result_context(
    *,
    catalog_version: int,
    metric_ids: Sequence[str],
    as_of_date: date,
    scope_summary: str,
    final_sql: str,
    table: ResultTable,
) -> str:
    """按模块文档 BI_RESULT_V1 格式渲染；输出稳定（同输入同输出）。"""
    lines = [RESULT_CONTEXT_HEADER, f"catalog_version: {catalog_version}", "metric_ids: " + ",".join(metric_ids), f"as_of_date: {as_of_date}", f"scope: {scope_summary}", f"sql: {final_sql}", "columns: " + " | ".join(_escape(c.name) for c in table.columns)]
    lines += ["row: " + " | ".join(_escape("NULL" if v is None else repr(v) if isinstance(v,float) else str(v)) for v in row) for row in table.rows]
    lines.append("truncated: " + str(table.source_truncated).lower())
    return "\n".join(lines)


def render_alert_context(alerts: Sequence[InventoryAlert]) -> str:
    """按模块文档 BI_ALERTS_V1 格式渲染；alerts 为空时返回空字符串（agent 不生成该上下文块）。"""
    if not alerts:
        return ""
    lines = [ALERT_CONTEXT_HEADER]
    for a in alerts:
        lines.append("alert: " + " | ".join(_escape(v) for v in [a.alert_type.value, a.severity.value, a.warehouse_id, a.material_id, f"current={a.current_value}", f"threshold={a.threshold_value}", f"gap={a.gap_qty}", a.recommended_action.value, f"qty={a.suggested_qty}"]))
    return "\n".join(lines)


def parse_result_context(full_content: str) -> tuple[list[str], list[list[str]]] | None:
    """解析 BI_RESULT_V1，返回 (columns, rows 原始字符串单元格)；非该格式返回 None（不抛异常）。"""
    if not full_content.startswith(RESULT_CONTEXT_HEADER + "\n"):
        return None
    try:
        lines = full_content.splitlines()
        columns = _split_cells(next(l[9:] for l in lines if l.startswith("columns: ")))
        rows = [_split_cells(l[5:]) for l in lines if l.startswith("row: ")]
        return (columns, rows) if all(len(r) == len(columns) for r in rows) else None
    except (ValueError, StopIteration):
        return None


def summarize_attempts_for_log(attempts: Sequence[SqlAttempt]) -> list[dict[str, str]]:
    """审计日志用的尝试摘要：attempt_index / outcome / 违规码 / 执行状态 / 耗时；不含 SQL 原文与结果数据。"""
    return [{"attempt_index": str(a.attempt_index), "outcome": a.outcome.value, "violations": ",".join(v.code.value for v in a.validation.violations), "execution_status": a.execution.status.value if a.execution else "-", "elapsed_ms": f"{a.elapsed_ms:.3f}"} for a in attempts]


def extract_numeric_tokens(text: str) -> list[tuple[float, int, bool]]:
    """抽取 (数值, 小数位数, 是否百分号)；剔除 YYYY-MM-DD / YYYY年M月D日 / 含字母编码；千分位逗号去除。
    agent._verify_numeric_consistency 与 eval_judge.DataAnalysisJudge 共用此函数，保证两侧数字口径一致。"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\d{4}-\d{1,2}-\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日", " ", text)
    text = re.sub(r"[A-Za-z_][A-Za-z_0-9]*(?:-[A-Za-z_0-9]+)*", " ", text)
    result = []
    for match in re.finditer(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*%?|(?<=[\u3400-\u9fff])[-+]?\d[\d,]*(?:\.\d+)?\s*%?", text):
        raw = match.group().strip()
        percent = raw.endswith('%')
        value = raw.rstrip('%').strip().replace(',', '')
        decimals = len(value.partition('.')[2])
        result.append((float(value), decimals, percent))
    return result


def numeric_match(token: tuple[float, int, bool], evidence: float) -> bool:
    """容差规则（同 eval_judge 模块文档）：|a − e| ≤ 0.5 × 10^-d；百分号 token 额外尝试 a / 100。"""
    value, decimals, percent = token
    if not math.isfinite(value) or not math.isfinite(evidence):
        return False
    tolerance = 0.5 * 10 ** -decimals
    return abs(value - evidence) <= tolerance + 1e-12 or (percent and abs(value / 100 - evidence) <= tolerance / 100 + 1e-12)


def _metric_column(column, metric_id):
    aliases = {"material_loss_rate": {"loss_rate"}, "safety_stock_gap": {"gap", "gap_qty"}, "shelf_life_turnover_days": {"turnover_days"}, "store_cups_sold": {"cups_sold", "total_cups"}, "near_expiry_qty": {"remaining_qty"}, "days_of_supply": {"dos"}}
    return column == metric_id or column in aliases.get(metric_id, set())


def _escape(value):
    return value.replace('\\', '\\\\').replace('|', '\\|').replace('\n', '\\n').replace('\r', '\\r')


def _split_cells(line):
    cells = line.split(' | ')
    return [re.sub(r'\\([\\|nr])', lambda m: {'n': '\n', 'r': '\r'}.get(m[1], m[1]), c) for c in cells]
