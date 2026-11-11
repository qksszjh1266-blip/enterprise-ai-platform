"""读取已提交的评测报告并聚合为看板数据，只读取，不重新运行也不改写任何报告。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "eval_platform/reports"
BI_DATA = ROOT / "tests/agent_matrix/supply_chain_bi/data"
FINANCE_REPORT = ROOT / "tests/agent_matrix/finance_audit/data/finance_audit_report.md"

FAILURE_LABELS = {"none": "通过", "retrieval_miss": "召回缺失", "ranking_low": "排序偏低", "acl_leak": "越权泄露",
                  "hallucination": "事实幻觉", "incomplete_answer": "回答不完整", "answer_incorrect": "回答错误",
                  "low_satisfaction": "满意度低", "degraded_service": "服务降级", "system_error": "系统错误",
                  "timeout": "超时", "judge_error": "裁判异常"}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _agent(key: str, label: str, record: dict[str, Any], note: str = "") -> dict[str, Any]:
    s = record["summary"]
    return {"key": key, "label": label, "run_id": record["run_id"], "version": record["config"]["system_version"],
            "samples": s["total_samples"], "failed": s["failed_samples"], "accuracy": s["answer_accuracy_rate"],
            "hit_rate": s["retrieval_hit_rate"], "recall": s["mean_recall_at_k"], "mrr": s["mean_mrr"],
            "faithfulness": s["mean_faithfulness"], "error_rate": s["error_rate"], "p95_ms": s["latency_p95_ms"],
            "acl_total": s["acl_redline_total"], "acl_blocked": s["acl_redline_blocked"],
            "failure_distribution": s["failure_distribution"], "note": note}


def _gate(label: str, kind: str, diff: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "kind": kind, "baseline": diff["baseline_version"], "candidate": diff["candidate_version"],
            "passed": diff["gate_passed"], "reasons": diff["blocking_reasons"],
            "violations": [v["code"] for v in diff["violations"]], "newly_failed": len(diff["newly_failed_sample_ids"]),
            "bad_cases": [{"sample_id": c["sample_id"], "category": c["failure_category"], "detail": c["detail"]}
                          for c in diff["top_bad_cases"][:3]]}


def _percent(cell: str) -> float | None:
    match = re.match(r"([\d.]+)%", cell.strip())
    return float(match.group(1)) / 100 if match else None


def _number(cell: str) -> float | None:
    match = re.match(r"([\d.]+)", cell.strip())
    return float(match.group(1)) if match else None


def parse_finance_report(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, int]]]:
    """财务评测只保存 Markdown，按报告模板解析基线指标与各故障注入门禁结论。"""
    sections = re.split(r"(?m)^# 智能体运营评估报告\s*$", text)[1:]
    if not sections:
        raise ValueError("财务评测报告格式无法识别")
    parsed = []
    for section in sections:
        rows = {m.group(1).strip(): m.group(2) for m in re.finditer(r"(?m)^\| ([^|]+) \| ([^|]+) \|", section)}
        run = re.search(r"运行 ID：(\S+)", section).group(1)
        samples = int(re.search(r"样本数：(\d+)", section).group(1))
        passed = "通过" in re.search(r"门禁：([^\n]*)", section).group(1)
        slice_row = re.search(r"(?m)^\| 财务审批 \| (\d+) \| (\d+) \|.*\| (\d+)/(\d+) \|\s*$", section)
        block = section.split("## 坏例归因分布", 1)[-1].split("## Top Bad Cases", 1)[0]
        distribution = {m.group(1): int(m.group(2)) for m in re.finditer(r"(?m)^\| ([a-z_]+) \| (\d+) \|", block)}
        distribution["none"] = samples - sum(distribution.values())
        parsed.append({"run": run, "samples": samples, "passed": passed, "rows": rows, "slice": slice_row,
                       "distribution": distribution, "section": section})
    base = parsed[0]
    failed = int(base["slice"].group(2)) if base["slice"] else 0
    agent = {"key": "finance_audit", "label": "财务合同预审", "run_id": base["run"], "version": "baseline",
             "samples": base["samples"], "failed": failed, "accuracy": _percent(base["rows"].get("回答准确率", "")),
             "hit_rate": _percent(base["rows"].get("检索命中率", "")), "recall": _percent(base["rows"].get("平均召回率", "")),
             "mrr": _number(base["rows"].get("MRR", "")), "faithfulness": _percent(base["rows"].get("忠实度", "")),
             "error_rate": _percent(base["rows"].get("错误率", "")), "p95_ms": _number(base["rows"].get("P95", "")),
             "acl_total": int(base["slice"].group(4)) if base["slice"] else 0,
             "acl_blocked": int(base["slice"].group(3)) if base["slice"] else 0,
             "failure_distribution": base["distribution"], "note": ""}
    names = {"finance-split-rule-missing": "财务拆单规则放水", "finance-privacy-defect": "财务隐私泄露注入",
             "finance-acl-context-injection": "财务越权制度注入", "finance-model-outage": "财务模型故障",
             "finance-amount-hallucination": "财务金额幻觉"}
    gates = []
    for item in parsed:
        conclusion = re.search(r"## 上线门禁结论\n+.*?\n\n(.*?)\n## 分场景", item["section"], re.S)
        reasons = re.findall(r"(?m)^- (.+)$", conclusion.group(1)) if conclusion else []
        baseline = item is base
        gates.append({"label": "财务基线自比较" if baseline else names.get(item["run"], item["run"]),
                      "kind": "基线" if baseline else "故障注入", "baseline": "baseline", "candidate": item["run"],
                      "passed": item["passed"], "reasons": reasons, "violations": [], "newly_failed": None, "bad_cases": []})
    return agent, gates, [item["distribution"] for item in parsed[1:]]


def build_dashboard() -> dict[str, Any]:
    retrieval = _json(REPORTS / "rule_e2e_run.json")
    sop = _json(REPORTS / "store_sop_baseline.json")
    bi = _json(BI_DATA / "supply_chain_bi_baseline.json")
    finance_agent, finance_gates, finance_injected = parse_finance_report(FINANCE_REPORT.read_text(encoding="utf-8"))
    agents = [
        _agent("retrieval", "知识检索端到端", retrieval),
        _agent("store_sop", "门店 SOP 督导", sop, "忠实度偏低源于固定规则裁判不识别引用标记与拒答模板，并非模型幻觉率，原始分保留。"),
        _agent("supply_chain_bi", "供应链经营分析", bi),
        finance_agent,
    ]
    gates = [
        _gate("检索精排对比", "精排实验", _json(REPORTS / "regression_diff.json")),
        _gate("SOP 越权注入", "故障注入", _json(REPORTS / "store_sop_acl_diff.json")),
        _gate("SOP 数值篡改", "故障注入", _json(REPORTS / "store_sop_numeric_diff.json")),
        _gate("BI 越权注入", "故障注入", _json(BI_DATA / "supply_chain_bi_acl_diff.json")),
        _gate("BI 数值篡改", "故障注入", _json(BI_DATA / "supply_chain_bi_numeric_diff.json")),
        *finance_gates,
    ]
    candidates = [_json(REPORTS / "store_sop_acl_candidate.json"), _json(REPORTS / "store_sop_numeric_candidate.json"),
                  _json(BI_DATA / "supply_chain_bi_acl_candidate.json"), _json(BI_DATA / "supply_chain_bi_numeric_candidate.json")]
    injected: dict[str, int] = {}
    for distribution in [*(record["summary"]["failure_distribution"] for record in candidates), *finance_injected]:
        for key, value in distribution.items():
            injected[key] = injected.get(key, 0) + value
    total_acl = sum(a["acl_total"] for a in agents)
    blocked_acl = sum(a["acl_blocked"] for a in agents)
    return {
        "kpis": {"samples": sum(a["samples"] for a in agents), "acl_total": total_acl, "acl_blocked": blocked_acl,
                 "gates_total": len(gates), "gates_passed": sum(g["passed"] for g in gates),
                 "gates_blocked": sum(not g["passed"] for g in gates),
                 "worst_p95_ms": max(a["p95_ms"] for a in agents if a["p95_ms"] is not None)},
        "agents": agents, "gates": gates, "injected_failure_distribution": injected, "failure_labels": FAILURE_LABELS,
        "disclaimer": "报告来自合成语料、内存检索替身与确定性模型，不代表生产模型准确率、真人满意度或压力测试结果。",
    }
