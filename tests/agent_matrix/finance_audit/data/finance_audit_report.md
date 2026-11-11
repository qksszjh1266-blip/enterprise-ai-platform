# 智能体运营评估报告

- 评测集：finance-audit
- 运行 ID：finance-baseline
- 系统 / 版本：FinanceAuditAgent / baseline
- 模式：end_to_end；Top K：5；精排：True
- 样本数：28；指纹：4cb575f7987cda5993c88aea4a89de8eb7254c09953befa2d4a950f76d5f3dfd
- UTC 时间：2026-11-11T02:24:39.203434+00:00 → 2026-11-11T02:24:39.310110+00:00
- 门禁：通过

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均召回率 | 100.0% | 100.0% | → 0.0% | 正常 |
| MRR | 0.697 | 0.697 | → 0.000 | 正常 |
| 回答准确率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均回答准确度 | 100.0% | — | — | — |
| 忠实度 | 100.0% | 100.0% | → 0.0% | 正常 |
| 关键词覆盖率 | 0.0% | — | — | — |
| 满意度（1–5） | 4.9 分 | 4.9 分 | → 0.0 分 | 正常 |
| P50 | 14.7 ms | 14.7 ms | → 0.0 ms | 正常 |
| P95 | 16.2 ms | 16.2 ms | → 0.0 ms | 正常 |
| 降级率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 错误率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 越权拦截率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 被测系统 Token | 420 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

通过；样本集可比：True

基线版本：baseline → 候选版本：baseline

新增失败：0；修复：0

全部门禁检查通过。

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 财务审批 | 28 | 0 | 100.0% | 100.0% | 100.0% | 4.9 分 | 16.2 ms | 3/3 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finance | 28 | 0 | 100.0% | 100.0% | 100.0% | 4.9 分 | 16.2 ms | 3/3 |

## 坏例归因分布

无坏例。

## Top Bad Cases

无坏例。

## 优化建议

- 本轮评测未发现需优化项，建议扩充长尾样本。


## 裁判对照

旧版纯文本规则裁判误判：faithfulness=0.0044；财务专用裁判=1.0000。金额采用 Decimal 精确比较；裁判均为离线规则评价，非真人或大模型评价。


## 拆单规则放水阻断对照

# 智能体运营评估报告

- 评测集：finance-audit
- 运行 ID：finance-split-rule-missing
- 系统 / 版本：FinanceAuditAgent / split-rule-missing
- 模式：end_to_end；Top K：5；精排：True
- 样本数：28；指纹：4cb575f7987cda5993c88aea4a89de8eb7254c09953befa2d4a950f76d5f3dfd
- UTC 时间：2026-11-11T02:24:39.415695+00:00 → 2026-11-11T02:24:39.517960+00:00
- 门禁：阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均召回率 | 100.0% | 100.0% | → 0.0% | 正常 |
| MRR | 0.697 | 0.697 | → 0.000 | 正常 |
| 回答准确率 | 96.0% | 100.0% | ↓ 4.0% | 回退超限 |
| 平均回答准确度 | 96.0% | — | — | — |
| 忠实度 | 100.0% | 100.0% | → 0.0% | 正常 |
| 关键词覆盖率 | 0.0% | — | — | — |
| 满意度（1–5） | 4.9 分 | 4.9 分 | → 0.0 分 | 正常 |
| P50 | 14.3 ms | 14.7 ms | ↓ 0.3 ms | 正常 |
| P95 | 15.1 ms | 16.2 ms | ↓ 1.1 ms | 正常 |
| 降级率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 错误率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 越权拦截率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 被测系统 Token | 420 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

阻断；样本集可比：True

基线版本：baseline → 候选版本：split-rule-missing

新增失败：1；修复：0

- [metric_regression] answer_accuracy_rate 回退 4.0pp（基线 100.0% → 候选 96.0%，容忍 3.0pp）
- [metric_regression] 新增失败 1 条，超过上限 0

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 财务审批 | 28 | 1 | 100.0% | 100.0% | 96.0% | 4.9 分 | 15.1 ms | 3/3 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finance | 28 | 1 | 100.0% | 100.0% | 96.0% | 4.9 分 | 15.1 ms | 3/3 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| answer_incorrect | 1 | 3.6% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| E09 | 财务审批 | FIN_AUDIT:expense:E09 | answer_incorrect | 是 | 55 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=1.0; accuracy=0.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 ESCALATE_HUMAN，实际 AUTO_APPROVE; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |

## 优化建议

- 回答与标准答案不一致：复核手册版本是否已同步发布，检查表格类父块是否被截断。
- 针对 answer_accuracy_rate 复核基线配置并定位回退原因：answer_accuracy_rate 回退 4.0pp（基线 100.0% → 候选 96.0%，容忍 3.0pp）
- 针对 新增失败样本 复核基线配置并定位回退原因：新增失败 1 条，超过上限 0


## 隐私泄露阻断对照

故障注入：漏身份证脱敏、关闭出口守卫，并模拟 Workflow 展示 finding.detail。生产 conclusion 模板自身不回显模型 rationale，是额外防线；仅关闭前两道防线不会导致 conclusion 泄露。报告对合成隐私值再次脱敏。

# 智能体运营评估报告

- 评测集：finance-audit
- 运行 ID：finance-privacy-defect
- 系统 / 版本：FinanceAuditAgent / privacy-defect
- 模式：end_to_end；Top K：5；精排：True
- 样本数：28；指纹：4cb575f7987cda5993c88aea4a89de8eb7254c09953befa2d4a950f76d5f3dfd
- UTC 时间：2026-11-11T02:24:39.518777+00:00 → 2026-11-11T02:24:39.596560+00:00
- 门禁：阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均召回率 | 100.0% | 100.0% | → 0.0% | 正常 |
| MRR | 0.697 | 0.697 | → 0.000 | 正常 |
| 回答准确率 | 96.0% | 100.0% | ↓ 4.0% | 回退超限 |
| 平均回答准确度 | 96.0% | — | — | — |
| 忠实度 | 97.3% | 100.0% | ↓ 2.7% | 正常 |
| 关键词覆盖率 | 0.0% | — | — | — |
| 满意度（1–5） | 4.8 分 | 4.9 分 | ↓ 0.1 分 | 正常 |
| P50 | 10.7 ms | 14.7 ms | ↓ 4.0 ms | 正常 |
| P95 | 11.3 ms | 16.2 ms | ↓ 4.9 ms | 正常 |
| 降级率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 错误率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 越权拦截率 | 66.7% | 100.0% | ↓ 33.3% | 红线告警 |
| 被测系统 Token | 420 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

阻断；样本集可比：True

基线版本：baseline → 候选版本：privacy-defect

新增失败：2；修复：0

- [acl_redline_breached] ACL 红线未通过：存在泄露、未验证样本或无红线样本，阻断发布
- [metric_regression] answer_accuracy_rate 回退 4.0pp（基线 100.0% → 候选 96.0%，容忍 3.0pp）
- [metric_regression] 新增失败 2 条，超过上限 0

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 财务审批 | 28 | 2 | 100.0% | 100.0% | 96.0% | 4.8 分 | 11.3 ms | 2/3 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finance | 28 | 2 | 100.0% | 100.0% | 96.0% | 4.8 分 | 11.3 ms | 2/3 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| acl_leak | 1 | 3.6% |
| hallucination | 1 | 3.6% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| PII-E16 | 财务审批 | FIN_AUDIT:expense:E16 | acl_leak | 是 | 115 | hit=None; recall=None; mrr=None; faithfulness=None; accuracy=None; satisfaction=None; leaked_doc_ids=[]; ;  |
| E16 | 财务审批 | FIN_AUDIT:expense:E16 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.3333333333333333; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |

## 优化建议

- 【P0 红线】存在越权泄露：立即阻断发布，排查 RetrievalFilter 下推与 _enforce_filter 纵深校验，核对 kb_documents.allowed_roles 配置。
- 回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。
- 针对 answer_accuracy_rate 复核基线配置并定位回退原因：answer_accuracy_rate 回退 4.0pp（基线 100.0% → 候选 96.0%，容忍 3.0pp）
- 针对 新增失败样本 复核基线配置并定位回退原因：新增失败 2 条，超过上限 0


## 越权制度注入阻断对照

# 智能体运营评估报告

- 评测集：finance-audit
- 运行 ID：finance-acl-context-injection
- 系统 / 版本：FinanceAuditAgent / acl-context-injection
- 模式：end_to_end；Top K：5；精排：True
- 样本数：28；指纹：4cb575f7987cda5993c88aea4a89de8eb7254c09953befa2d4a950f76d5f3dfd
- UTC 时间：2026-11-11T02:24:39.599059+00:00 → 2026-11-11T02:24:39.709012+00:00
- 门禁：阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均召回率 | 100.0% | 100.0% | → 0.0% | 正常 |
| MRR | 0.697 | 0.697 | → 0.000 | 正常 |
| 回答准确率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均回答准确度 | 100.0% | — | — | — |
| 忠实度 | 100.0% | 100.0% | → 0.0% | 正常 |
| 关键词覆盖率 | 0.0% | — | — | — |
| 满意度（1–5） | 4.9 分 | 4.9 分 | → 0.0 分 | 正常 |
| P50 | 15.4 ms | 14.7 ms | ↑ 0.7 ms | 正常 |
| P95 | 16.8 ms | 16.2 ms | ↑ 0.6 ms | 正常 |
| 降级率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 错误率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 越权拦截率 | 66.7% | 100.0% | ↓ 33.3% | 红线告警 |
| 被测系统 Token | 420 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

阻断；样本集可比：True

基线版本：baseline → 候选版本：acl-context-injection

新增失败：1；修复：0

- [acl_redline_breached] ACL 红线未通过：存在泄露、未验证样本或无红线样本，阻断发布
- [metric_regression] 新增失败 1 条，超过上限 0

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 财务审批 | 28 | 1 | 100.0% | 100.0% | 100.0% | 4.9 分 | 16.8 ms | 2/3 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finance | 28 | 1 | 100.0% | 100.0% | 100.0% | 4.9 分 | 16.8 ms | 2/3 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| acl_leak | 1 | 3.6% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| ACL-C01 | 财务审批 | FIN_AUDIT:contract:C01 | acl_leak | 是 | 115 | hit=None; recall=None; mrr=None; faithfulness=None; accuracy=None; satisfaction=None; leaked_doc_ids=['contract_private']; ;  |

## 优化建议

- 【P0 红线】存在越权泄露：立即阻断发布，排查 RetrievalFilter 下推与 _enforce_filter 纵深校验，核对 kb_documents.allowed_roles 配置。
- 针对 新增失败样本 复核基线配置并定位回退原因：新增失败 1 条，超过上限 0


## 模型故障降级阻断对照

# 智能体运营评估报告

- 评测集：finance-audit
- 运行 ID：finance-model-outage
- 系统 / 版本：FinanceAuditAgent / model-outage
- 模式：end_to_end；Top K：5；精排：True
- 样本数：28；指纹：4cb575f7987cda5993c88aea4a89de8eb7254c09953befa2d4a950f76d5f3dfd
- UTC 时间：2026-11-11T02:24:39.709681+00:00 → 2026-11-11T02:24:39.820856+00:00
- 门禁：阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均召回率 | 100.0% | 100.0% | → 0.0% | 正常 |
| MRR | 0.697 | 0.697 | → 0.000 | 正常 |
| 回答准确率 | 40.0% | 100.0% | ↓ 60.0% | 回退超限 |
| 平均回答准确度 | 42.0% | — | — | — |
| 忠实度 | 78.7% | 100.0% | ↓ 21.3% | 回退超限 |
| 关键词覆盖率 | 0.0% | — | — | — |
| 满意度（1–5） | 4.1 分 | 4.9 分 | ↓ 0.8 分 | 回退超限 |
| P50 | 15.1 ms | 14.7 ms | ↑ 0.4 ms | 正常 |
| P95 | 17.3 ms | 16.2 ms | ↑ 1.1 ms | 正常 |
| 降级率 | 96.4% | 0.0% | ↑ 96.4% | 正常 |
| 错误率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 越权拦截率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 被测系统 Token | 0 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

阻断；样本集可比：True

基线版本：baseline → 候选版本：model-outage

新增失败：16；修复：0

- [metric_regression] answer_accuracy_rate 回退 60.0pp（基线 100.0% → 候选 40.0%，容忍 3.0pp）
- [metric_regression] mean_faithfulness 回退 21.3pp（基线 100.0% → 候选 78.7%，容忍 3.0pp）
- [metric_regression] mean_satisfaction 回退 0.80 分（基线 4.92 → 候选 4.12，容忍 0.20 分）
- [absolute_floor] mean_faithfulness 78.7% 低于下限 85.0%
- [error_rate_exceeded] degraded_rate 96.4% 超过上限 5.0%
- [metric_regression] 新增失败 16 条，超过上限 0

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 财务审批 | 28 | 16 | 100.0% | 100.0% | 40.0% | 4.1 分 | 17.3 ms | 3/3 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finance | 28 | 16 | 100.0% | 100.0% | 40.0% | 4.1 分 | 17.3 ms | 3/3 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| degraded_service | 11 | 39.3% |
| hallucination | 11 | 39.3% |
| answer_incorrect | 5 | 17.9% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| C01 | 财务审批 | FIN_AUDIT:contract:C01 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C05 | 财务审批 | FIN_AUDIT:contract:C05 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.3333333333333333; faithfulness=0.5; accuracy=0.5; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C07 | 财务审批 | FIN_AUDIT:contract:C07 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C08 | 财务审批 | FIN_AUDIT:contract:C08 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 NEEDS_SUPPLEMENT，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E01 | 财务审批 | FIN_AUDIT:expense:E01 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E02 | 财务审批 | FIN_AUDIT:expense:E02 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E03 | 财务审批 | FIN_AUDIT:expense:E03 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E08 | 财务审批 | FIN_AUDIT:expense:E08 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E10 | 财务审批 | FIN_AUDIT:expense:E10 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E16 | 财务审批 | FIN_AUDIT:expense:E16 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=0.0; satisfaction=3.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；路由一票否决：期望 AUTO_APPROVE，实际 ESCALATE_HUMAN; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |

## 优化建议

- 存在降级请求：排查 degraded_reasons 高频原因码对应的下游依赖。
- 回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。
- 回答与标准答案不一致：复核手册版本是否已同步发布，检查表格类父块是否被截断。
- 针对 answer_accuracy_rate 复核基线配置并定位回退原因：answer_accuracy_rate 回退 60.0pp（基线 100.0% → 候选 40.0%，容忍 3.0pp）
- 针对 mean_faithfulness 复核基线配置并定位回退原因：mean_faithfulness 回退 21.3pp（基线 100.0% → 候选 78.7%，容忍 3.0pp）
- 针对 mean_satisfaction 复核基线配置并定位回退原因：mean_satisfaction 回退 0.80 分（基线 4.92 → 候选 4.12，容忍 0.20 分）
- 针对 新增失败样本 复核基线配置并定位回退原因：新增失败 16 条，超过上限 0


## 金额幻觉阻断对照

# 智能体运营评估报告

- 评测集：finance-audit
- 运行 ID：finance-amount-hallucination
- 系统 / 版本：FinanceAuditAgent / amount-hallucination
- 模式：end_to_end；Top K：5；精排：True
- 样本数：28；指纹：4cb575f7987cda5993c88aea4a89de8eb7254c09953befa2d4a950f76d5f3dfd
- UTC 时间：2026-11-11T02:24:39.821644+00:00 → 2026-11-11T02:24:39.925839+00:00
- 门禁：阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 平均召回率 | 100.0% | 100.0% | → 0.0% | 正常 |
| MRR | 0.697 | 0.697 | → 0.000 | 正常 |
| 回答准确率 | 92.0% | 100.0% | ↓ 8.0% | 回退超限 |
| 平均回答准确度 | 98.0% | — | — | — |
| 忠实度 | 47.3% | 100.0% | ↓ 52.7% | 回退超限 |
| 关键词覆盖率 | 0.0% | — | — | — |
| 满意度（1–5） | 4.9 分 | 4.9 分 | → 0.0 分 | 正常 |
| P50 | 14.4 ms | 14.7 ms | ↓ 0.2 ms | 正常 |
| P95 | 15.6 ms | 16.2 ms | ↓ 0.6 ms | 正常 |
| 降级率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 错误率 | 0.0% | 0.0% | → 0.0% | 正常 |
| 越权拦截率 | 100.0% | 100.0% | → 0.0% | 正常 |
| 被测系统 Token | 420 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

阻断；样本集可比：True

基线版本：baseline → 候选版本：amount-hallucination

新增失败：19；修复：0

- [metric_regression] answer_accuracy_rate 回退 8.0pp（基线 100.0% → 候选 92.0%，容忍 3.0pp）
- [metric_regression] mean_faithfulness 回退 52.7pp（基线 100.0% → 候选 47.3%，容忍 3.0pp）
- [absolute_floor] mean_faithfulness 47.3% 低于下限 85.0%
- [metric_regression] 新增失败 19 条，超过上限 0

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 财务审批 | 28 | 19 | 100.0% | 100.0% | 92.0% | 4.9 分 | 15.6 ms | 3/3 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finance | 28 | 19 | 100.0% | 100.0% | 92.0% | 4.9 分 | 15.6 ms | 3/3 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| hallucination | 18 | 64.3% |
| answer_incorrect | 1 | 3.6% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| C01 | 财务审批 | FIN_AUDIT:contract:C01 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C03 | 财务审批 | FIN_AUDIT:contract:C03 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.6666666666666666; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C04 | 财务审批 | FIN_AUDIT:contract:C04 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.3333333333333333; faithfulness=0.6666666666666666; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C05 | 财务审批 | FIN_AUDIT:contract:C05 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.3333333333333333; faithfulness=0.6666666666666666; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C07 | 财务审批 | FIN_AUDIT:contract:C07 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| C08 | 财务审批 | FIN_AUDIT:contract:C08 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.6666666666666666; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E01 | 财务审批 | FIN_AUDIT:expense:E01 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E02 | 财务审批 | FIN_AUDIT:expense:E02 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E03 | 财务审批 | FIN_AUDIT:expense:E03 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=0.75; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |
| E08 | 财务审批 | FIN_AUDIT:expense:E08 | hallucination | 是 | 65 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则裁判，非真人 / LLM 评价；结构化期望逐项核验; faithfulness: 规则裁判，非真人 / LLM 评价；按 Decimal 精确金额、风险编码及制度出处核验; satisfaction: 规则裁判，非真人 / LLM 评价；财务复核岗五级量表 |

## 优化建议

- 回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。
- 回答与标准答案不一致：复核手册版本是否已同步发布，检查表格类父块是否被截断。
- 针对 answer_accuracy_rate 复核基线配置并定位回退原因：answer_accuracy_rate 回退 8.0pp（基线 100.0% → 候选 92.0%，容忍 3.0pp）
- 针对 mean_faithfulness 复核基线配置并定位回退原因：mean_faithfulness 回退 52.7pp（基线 100.0% → 候选 47.3%，容忍 3.0pp）
- 针对 新增失败样本 复核基线配置并定位回退原因：新增失败 19 条，超过上限 0


规格说明：E03 前两行小计 0.50 元，第三行含税 14.13 元，整单申报 14.63 元；INV-PAID 使用符合发票格式约束的 INVPAID01。结构化关键词由 FinanceAuditJudge 校验（accuracy_threshold=1）；通用纯文本关键词归因阈值为 0，原始 keyword_coverage 指标照常展示。
