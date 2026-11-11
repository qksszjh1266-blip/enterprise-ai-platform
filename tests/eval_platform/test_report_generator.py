import pytest
from eval_platform.models import FailureCategory as F
from eval_platform.report_generator import MarkdownReportGenerator, RECOMMENDATION_RULES
from eval_platform.runner import RegressionGate
from tests.eval_platform.conftest import record, result


def test_section_order_and_without_diff():
    renderer=MarkdownReportGenerator()
    run=record()
    diff=RegressionGate().compare(run,run)
    report=renderer.render(run,diff)
    headings=['# 智能体运营评估报告','## 核心指标看板','## 上线门禁结论','## 分场景表现','## 分部门表现','## 坏例归因分布','## Top Bad Cases','## 优化建议']
    assert [report.index(h) for h in headings]==sorted(report.index(h) for h in headings)
    report=renderer.render(run)
    assert '上线门禁结论' not in report and '— 无基线' in report


def test_na_and_redline_alert():
    report=MarkdownReportGenerator().render(record([result()]))
    assert 'N/A' in report and '🚨 红线告警' in report


def test_table_escaping_and_stability():
    renderer=MarkdownReportGenerator()
    run=record([result(question='A|B\nC',failure_category=F.JUDGE_ERROR,judge_reasons={'accuracy':'<script>|oops\nnext'})])
    report=renderer.render(run)
    assert 'A&#124;B<br>C' in report
    assert '&lt;script&gt;&#124;oops<br>next' in report
    section=report.split('## Top Bad Cases')[1].split('## 优化建议')[0]
    assert all(line.count('|')==8 for line in section.splitlines() if line.startswith('|'))
    assert renderer.render(run)==report


def test_recommendations_priority_and_fallback():
    renderer=MarkdownReportGenerator()
    run=record([result('a',failure_category=F.ACL_LEAK,acl_leaked=True),*[result(str(i),failure_category=F.SYSTEM_ERROR) for i in range(4)]])
    recs=renderer.build_recommendations(run.summary)
    assert recs[0]==RECOMMENDATION_RULES[F.ACL_LEAK]
    assert RECOMMENDATION_RULES[F.SYSTEM_ERROR] in recs
    assert renderer.build_recommendations(record().summary)==['本轮评测未发现需优化项，建议扩充长尾样本。']
    b,c=record(),record(mean_mrr=.5,latency_p95_ms=20)
    recs=renderer.build_recommendations(c.summary,RegressionGate().compare(b,c))
    assert any('mean_mrr' in r for r in recs) and any('latency_p95_ms' in r for r in recs)


async def test_write_and_run_mismatch(tmp_path):
    renderer=MarkdownReportGenerator()
    run=record()
    report=renderer.render(run)
    path=tmp_path/'nested'/'report.md'
    await renderer.write(path,report)
    assert path.read_text()==report
    diff=RegressionGate().compare(run,run)
    diff.candidate_run_id='other'
    with pytest.raises(ValueError): renderer.render(run,diff)
