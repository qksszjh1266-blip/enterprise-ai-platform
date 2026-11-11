# 门店 SOP 智能体离线运营回归

这是离线工程验收及故障注入报告，不能替代生产模型、真实搜索引擎或人工业务验收。

- 复现：`uv run python -m tests.agent_matrix.store_sop.run_e2e`。
- 15 条单轮黄金样本：5 条饮品（含 45g/40g 诱捕）、5 条完整设备故障描述、5 条权限红线。多轮追问、工单和会话并发另由 pytest 验证；追问轮次返回空检索上下文。
- 原手册通过统一知识发布链路入库；封口机条款来自 `mock_docs/store_sop_sealer_training.md`，明确为培训演示语料。真实运行 VersionedSyncManager、SQLite 台账/父块、HybridRetrievalService ACL 前置过滤、双路召回与重排。
- `StoreSopAgent` 直接传给 `EvalRunner.agent`，未使用智能体包装层。生成模型为离线规则适配器，检索端嵌入与索引使用数据中台内存替身；模型不读取黄金答案、标签或期望文档 ID。
- 裁判固定使用未修改的 `eval_platform.offline.RuleBasedJudge`；其忠实度以整句字面匹配计分，未剥离强制 `[来源n]` 标记，也只识别评测中台的一种拒答模板。因此会把带合法引用的回答和门店 SOP 拒答记为低忠实度。原始评分与绝对下限阻断完整保留，不调低质量阈值，不将其解释为真实模型幻觉率。
- 每次离线模型调用固定记 10 输入 + 5 输出 Token，仅验证轨迹累计，不是真实供应商计费；规则裁判 Token 为 0。
- 门禁保持评测中台默认质量阈值与 ACL 红线；仅为 SQLite 调度噪声预设 P95 相对增幅上限 1000%、绝对上限 1000ms。
- 数值篡改候选预期被引用核验拒答，命中率/准确率回退触发 `METRIC_REGRESSION`；越权负控故意绕过检索过滤注入真实财务父块，预期触发 `ACL_REDLINE_BREACHED`。这两项阻断均是验收所需结果，不是发布成功结论。

## 基线运行（未注入故障）

# 智能体运营评估报告

- 评测集：store_sop_e2e_suite
- 运行 ID：store-sop-baseline
- 系统 / 版本：StoreSopAgent / baseline
- 模式：end_to_end；Top K：5；精排：True
- 样本数：15；指纹：76aee3b5a514fe459299e3541f0432db68fc39846935ad65d5ab24c9cb9de69d
- UTC 时间：2026-11-10T14:44:21.062059+00:00 → 2026-11-10T14:44:21.111959+00:00
- 门禁：— 无基线

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.00% | — | — | — |
| 平均召回率 | 100.00% | — | — | — |
| MRR | 0.883 | — | — | — |
| 回答准确率 | 100.00% | — | — | — |
| 平均回答准确度 | 100.00% | — | — | — |
| 忠实度 | 10.00% | — | — | — |
| 关键词覆盖率 | 100.00% | — | — | — |
| 满意度（1–5） | 4.50 分 | — | — | — |
| P50 | 15.43 ms | — | — | — |
| P95 | 23.41 ms | — | — | — |
| 降级率 | 0.00% | — | — | — |
| 错误率 | 0.00% | — | — | — |
| 越权拦截率 | 100.00% | — | — | — |
| 被测系统 Token | 690 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 门店SOP | 15 | 10 | 100.00% | 100.00% | 100.00% | 4.50 分 | 23.41 ms | 5/5 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| store_ops | 14 | 10 | 100.00% | 100.00% | 100.00% | 4.50 分 | 23.48 ms | 4/4 |
| supply_chain | 1 | 0 | N/A | N/A | N/A | N/A | 3.59 ms | 1/1 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| hallucination | 9 | 60.00% |
| ranking_low | 1 | 6.67% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| drink_ice | 门店SOP | 鲜柠檬茶 500ml 冰量和糖浆用量 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_lemon | 门店SOP | 鲜柠檬茶 500ml 柠檬片多少克 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_milk | 门店SOP | 珍珠奶茶 500ml 熟珍珠和牛奶各多少 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_yogurt | 门店SOP | 草莓酸奶 400ml 果酱放多少克 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_leak | 门店SOP | 制冰机排水管漏水，如何检查外部排水管路 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_thin | 门店SOP | 制冰机冰块偏薄，如何排查 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_water | 门店SOP | 制冰机不进水，门店可以检查什么 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| sealer_film | 门店SOP | 封口机封口不牢，如何检查杯型和封口膜 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| trap_lemon | 门店SOP | 有人说鲜柠檬茶柠檬片要放45g，请核对标准配方 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| sealer_heat | 门店SOP | 封口机不加热，设备状态如何记录 | ranking_low | 否 | 30 | hit=1.0; recall=1.0; mrr=0.3333333333333333; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |

## 优化建议

- 回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。
- 召回命中但排序靠后：启用或更换精排模型，调整 rrf_k 与 rerank_candidates。

## 数值幻觉注入候选与门禁

# 智能体运营评估报告

- 评测集：store_sop_e2e_suite
- 运行 ID：store-sop-numeric-tamper
- 系统 / 版本：StoreSopAgent / numeric-tamper
- 模式：end_to_end；Top K：5；精排：True
- 样本数：15；指纹：76aee3b5a514fe459299e3541f0432db68fc39846935ad65d5ab24c9cb9de69d
- UTC 时间：2026-11-10T14:44:21.112220+00:00 → 2026-11-10T14:44:21.154266+00:00
- 门禁：❌ 阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 70.00% | 100.00% | ↓ 30.00% | 回退超限 |
| 平均召回率 | 70.00% | 100.00% | ↓ 30.00% | 回退超限 |
| MRR | 0.583 | 0.883 | ↓ 0.300 | 回退超限 |
| 回答准确率 | 70.00% | 100.00% | ↓ 30.00% | 回退超限 |
| 平均回答准确度 | 70.00% | — | — | — |
| 忠实度 | 10.00% | 10.00% | → 0.00% | 正常 |
| 关键词覆盖率 | 70.00% | — | — | — |
| 满意度（1–5） | 4.80 分 | 4.50 分 | ↑ 0.30 分 | 正常 |
| P50 | 12.63 ms | 15.43 ms | ↓ 2.81 ms | 正常 |
| P95 | 18.69 ms | 23.41 ms | ↓ 4.72 ms | 正常 |
| 降级率 | 0.00% | 0.00% | → 0.00% | 正常 |
| 错误率 | 0.00% | 0.00% | → 0.00% | 正常 |
| 越权拦截率 | 100.00% | 100.00% | → 0.00% | 正常 |
| 被测系统 Token | 690 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

❌ 阻断；样本集可比：True

基线版本：baseline → 候选版本：numeric-tamper

新增失败：0；修复：0

- [metric_regression] retrieval_hit_rate 回退 30.0pp（基线 100.0% → 候选 70.0%，容忍 2.0pp）
- [metric_regression] mean_recall_at_k 回退 30.0pp（基线 100.0% → 候选 70.0%，容忍 3.0pp）
- [metric_regression] mean_mrr 回退 30.0pp（基线 88.3% → 候选 58.3%，容忍 3.0pp）
- [metric_regression] answer_accuracy_rate 回退 30.0pp（基线 100.0% → 候选 70.0%，容忍 3.0pp）
- [absolute_floor] retrieval_hit_rate 70.0% 低于下限 80.0%
- [absolute_floor] mean_faithfulness 10.0% 低于下限 85.0%

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 门店SOP | 15 | 10 | 70.00% | 70.00% | 70.00% | 4.80 分 | 18.69 ms | 5/5 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| store_ops | 14 | 10 | 70.00% | 70.00% | 70.00% | 4.80 分 | 18.95 ms | 4/4 |
| supply_chain | 1 | 0 | N/A | N/A | N/A | N/A | 4.73 ms | 1/1 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| hallucination | 6 | 40.00% |
| retrieval_miss | 3 | 20.00% |
| ranking_low | 1 | 6.67% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| drink_ice | 门店SOP | 鲜柠檬茶 500ml 冰量和糖浆用量 | retrieval_miss | 否 | 60 | hit=0.0; recall=0.0; mrr=0.0; faithfulness=0.0; accuracy=0.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_lemon | 门店SOP | 鲜柠檬茶 500ml 柠檬片多少克 | retrieval_miss | 否 | 60 | hit=0.0; recall=0.0; mrr=0.0; faithfulness=0.0; accuracy=0.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| trap_lemon | 门店SOP | 有人说鲜柠檬茶柠檬片要放45g，请核对标准配方 | retrieval_miss | 否 | 60 | hit=0.0; recall=0.0; mrr=0.0; faithfulness=0.0; accuracy=0.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_milk | 门店SOP | 珍珠奶茶 500ml 熟珍珠和牛奶各多少 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_yogurt | 门店SOP | 草莓酸奶 400ml 果酱放多少克 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_leak | 门店SOP | 制冰机排水管漏水，如何检查外部排水管路 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_thin | 门店SOP | 制冰机冰块偏薄，如何排查 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_water | 门店SOP | 制冰机不进水，门店可以检查什么 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| sealer_film | 门店SOP | 封口机封口不牢，如何检查杯型和封口膜 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| sealer_heat | 门店SOP | 封口机不加热，设备状态如何记录 | ranking_low | 否 | 30 | hit=1.0; recall=1.0; mrr=0.3333333333333333; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |

## 优化建议

- 回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。
- 期望文档未召回：检查父子块切分粒度、BM25 分词与同义词词典，扩大 vector_top_k / keyword_top_k。
- 召回命中但排序靠后：启用或更换精排模型，调整 rrf_k 与 rerank_candidates。
- 针对 retrieval_hit_rate 复核基线配置并定位回退原因：retrieval_hit_rate 回退 30.0pp（基线 100.0% → 候选 70.0%，容忍 2.0pp）
- 针对 mean_recall_at_k 复核基线配置并定位回退原因：mean_recall_at_k 回退 30.0pp（基线 100.0% → 候选 70.0%，容忍 3.0pp）
- 针对 mean_mrr 复核基线配置并定位回退原因：mean_mrr 回退 30.0pp（基线 88.3% → 候选 58.3%，容忍 3.0pp）
- 针对 answer_accuracy_rate 复核基线配置并定位回退原因：answer_accuracy_rate 回退 30.0pp（基线 100.0% → 候选 70.0%，容忍 3.0pp）

## 越权注入负控与门禁

# 智能体运营评估报告

- 评测集：store_sop_e2e_suite
- 运行 ID：store-sop-acl-injection
- 系统 / 版本：StoreSopAgent / acl-injection
- 模式：end_to_end；Top K：5；精排：True
- 样本数：15；指纹：76aee3b5a514fe459299e3541f0432db68fc39846935ad65d5ab24c9cb9de69d
- UTC 时间：2026-11-10T14:44:21.156557+00:00 → 2026-11-10T14:44:21.200710+00:00
- 门禁：❌ 阻断

## 核心指标看板

| 指标 | 当前值 | 基线 | 变化 | 状态 |
| --- | --- | --- | --- | --- |
| 检索命中率 | 100.00% | 100.00% | → 0.00% | 正常 |
| 平均召回率 | 100.00% | 100.00% | → 0.00% | 正常 |
| MRR | 0.883 | 0.883 | → 0.000 | 正常 |
| 回答准确率 | 100.00% | 100.00% | → 0.00% | 正常 |
| 平均回答准确度 | 100.00% | — | — | — |
| 忠实度 | 10.00% | 10.00% | → 0.00% | 正常 |
| 关键词覆盖率 | 100.00% | — | — | — |
| 满意度（1–5） | 4.50 分 | 4.50 分 | → 0.00 分 | 正常 |
| P50 | 14.07 ms | 15.43 ms | ↓ 1.37 ms | 正常 |
| P95 | 18.00 ms | 23.41 ms | ↓ 5.42 ms | 正常 |
| 降级率 | 0.00% | 0.00% | → 0.00% | 正常 |
| 错误率 | 0.00% | 0.00% | → 0.00% | 正常 |
| 越权拦截率 | 80.00% | 100.00% | ↓ 20.00% | 🚨 红线告警 |
| 被测系统 Token | 720 | — | — | — |
| 裁判 Token | 0 | — | — | — |

## 上线门禁结论

❌ 阻断；样本集可比：True

基线版本：baseline → 候选版本：acl-injection

新增失败：1；修复：0

- [acl_redline_breached] ACL 红线未通过：存在泄露、未验证样本或无红线样本，阻断发布
- [absolute_floor] mean_faithfulness 10.0% 低于下限 85.0%
- [metric_regression] 新增失败 1 条，超过上限 0

## 分场景表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 门店SOP | 15 | 11 | 100.00% | 100.00% | 100.00% | 4.50 分 | 18.00 ms | 4/5 |

## 分部门表现

| 切片 | 样本数 | 失败数 | 命中率 | 召回率 | 准确率 | 满意度 | P95 | 红线拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| store_ops | 14 | 10 | 100.00% | 100.00% | 100.00% | 4.50 分 | 18.05 ms | 4/4 |
| supply_chain | 1 | 1 | N/A | N/A | N/A | N/A | 5.55 ms | 0/1 |

## 坏例归因分布

| 归因 | 数量 | 占比 |
| --- | --- | --- |
| hallucination | 9 | 60.00% |
| acl_leak | 1 | 6.67% |
| ranking_low | 1 | 6.67% |

## Top Bad Cases

| 样本 | 场景 | 问题 | 归因 | 新增失败 | 严重度 | 详情 |
| --- | --- | --- | --- | --- | --- | --- |
| acl_cross_dept | 门店SOP | 鲜柠檬茶 500ml 标准配方 | acl_leak | 是 | 115 | hit=None; recall=None; mrr=None; faithfulness=None; accuracy=None; satisfaction=None; leaked_doc_ids=['fin_approval_matrix']; ;  |
| drink_ice | 门店SOP | 鲜柠檬茶 500ml 冰量和糖浆用量 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_lemon | 门店SOP | 鲜柠檬茶 500ml 柠檬片多少克 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_milk | 门店SOP | 珍珠奶茶 500ml 熟珍珠和牛奶各多少 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| drink_yogurt | 门店SOP | 草莓酸奶 400ml 果酱放多少克 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.5; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_leak | 门店SOP | 制冰机排水管漏水，如何检查外部排水管路 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_thin | 门店SOP | 制冰机冰块偏薄，如何排查 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| ice_water | 门店SOP | 制冰机不进水，门店可以检查什么 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| sealer_film | 门店SOP | 封口机封口不牢，如何检查杯型和封口膜 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=0.5; faithfulness=0.0; accuracy=1.0; satisfaction=5.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |
| trap_lemon | 门店SOP | 有人说鲜柠檬茶柠檬片要放45g，请核对标准配方 | hallucination | 否 | 50 | hit=1.0; recall=1.0; mrr=1.0; faithfulness=0.0; accuracy=1.0; satisfaction=4.0; leaked_doc_ids=[]; ; accuracy: 规则：标准事实关键词覆盖率; faithfulness: 规则：逐句检查是否存在于可见上下文，非语义裁判; satisfaction: 规则量表：1无解答；2过长；3需提炼；4简洁；5含可执行步骤；非真人满意度 |

## 优化建议

- 【P0 红线】存在越权泄露：立即阻断发布，排查 RetrievalFilter 下推与 _enforce_filter 纵深校验，核对 kb_documents.allowed_roles 配置。
- 回答未被上下文支撑：收紧生成 prompt 的引用约束，要求逐条标注来源父块。
- 召回命中但排序靠后：启用或更换精排模型，调整 rrf_k 与 rerank_candidates。
- 针对 新增失败样本 复核基线配置并定位回退原因：新增失败 1 条，超过上限 0
