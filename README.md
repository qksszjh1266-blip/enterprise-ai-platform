# 综合电商企业 AI 平台

面向综合电商企业（自营旗舰店 + 多平台第三方店铺 + 自营仓配中心）的企业级 AI 平台工程：**知识数据中台 + 智能体评测中台 + 三大业务智能体矩阵**。覆盖电商运营 SOP 问答与智能工单协同、经营数据分析与进销存预警、财务报销与采购合同审批辅助三类场景，把权限、证据、确定性规则、故障降级和回归门禁落实为可执行代码。

当前仓库提供可离线复现的工程实现与验收实验。“百万单级”是业务定位；已有报告使用合成语料、内存检索替身和确定性模型，并未验证生产并发容量或生产大模型效果。架构、复现方法与评测边界见下文；各模块接口约定由模型、端口和契约测试共同维护。

## 业务场景

| 模块 | 场景 | 关键护栏 |
|---|---|---|
| 知识数据中台 `data_platform` | 同系列 SKU 规格参数表（克、毫升、温度、秒等精确数值列）、多平台售后退换赔付 SOP、仓配设备与订单履约异常排查、仓储作业规范、财务报销制度 | 父块 2000 / 子块 400（重叠 50）字符，表格与代码块整体保护；向量 + BM25 混合召回；ACL 前置下推；RRF(k=60) + Rerank；SHA-256 增量热切换；OpenAPI 3.1 |
| 评测中台 `eval_platform` | 电商与企业内控黄金集 38 条（28 条业务问答 + 10 条越权红线）、电商运营 SOP 端到端 16 条 | `asyncio.Semaphore` 限流；Hit@K / Recall@K / MRR；双重越权扫描；百分比数值容差裁判；坏例自动归因 |
| 电商运营 SOP 问答与智能工单协同 `agent_matrix/store_sop` | 同系列商品规格参数与售后规则查询；热敏打单机 / 扫码枪等仓配设备与订单履约异常排障；库房漏电 / 冒烟、批量资损、监管投诉等高危异常直派 P1 工单 | HiAgent / Dify 双模工作流；单轮最多 8 步；每轮最多追问 2 个槽位、最多 2 轮；满 10 轮摘要压缩；表格行级数值防串行核验 |
| 经营数据分析与进销存预警 `agent_matrix/supply_chain_bi` | SKU 销量、仓库库存、批次效期 / 周转天数、商品货损率、安全库存缺口、库存可供天数 6 项标准指标；缺货 / 滞销临期并发预警 | 7 张白名单表；分仓 / 大区 / 总部三级权限；sqlglot 语法树静态拦截（禁非 SELECT 与供应商采购底价等敏感列）；SQLite 只读沙箱；最多 3 次报错自纠错 |
| 财务报销与采购合同审批辅助 `agent_matrix/finance_audit` | 部门费用报销；外部供应商 / 合作商的商品采购与仓储物流合同 | Python Decimal 19 项确定性规则（金额勾稽、重复发票与拆单、制度合规、合同账期）；7 天报销拆单 / 90 天同主体合同拆分检测；身份证、银行卡 Luhn、手机号跨字段一致占位符脱敏；账期与预付比例双向核验 |

## Monorepo 结构

```text
.
├── data_platform/                 # 知识发布与检索
│   ├── connectors/                # 连接器包占位
│   ├── parser_chunker/            # Markdown 层级父子块
│   ├── storage_sync/              # SQL 台账、父块、蓝绿同步、异步端口
│   ├── retrieval_engine/          # ACL、双路召回、RRF、回表、精排
│   ├── adapters/memory.py         # 内存子块检索/精排适配器
│   └── mcp_server/app.py          # FastAPI / OpenAPI 工具层
├── eval_platform/                 # 评分、聚合、归因、回归门禁
│   ├── golden_datasets/           # 电商与内控 38 条、电商运营 SOP 16 条黄金集
│   ├── reports/                   # 检索与电商运营 SOP 报告与逐样本 JSON
│   ├── evaluators.py              # 文档级检索指标与 ACL 检测
│   ├── runner.py                  # EvalRunner、RegressionGate
│   └── report_generator.py        # Markdown 运营报告
├── agent_matrix/
│   ├── store_sop/                 # 电商运营 SOP：多轮问答、引用核验、工单协同
│   ├── supply_chain_bi/           # 经营分析：语义目录、AST、只读 SQL、进销存预警
│   ├── finance_audit/             # 财务审批：Decimal 勾稽、脱敏、引用、审批路由
│   └── workflows/                 # 三份 Dify / HiAgent 编排结构说明
├── mcp_server/                    # 标准 MCP stdio Server（知识检索 Tool、指标目录 Tool、SOP 引用 Prompt）
├── tests/                         # 875 项参数化展开后的 pytest 测试
│   ├── parser_chunker/            # 15 项
│   ├── storage_sync/              # 28 项
│   ├── retrieval_engine/          # 44 项
│   ├── mcp_server/                # 16 项
│   ├── mcp_adapter/               # 79 项
│   ├── eval_platform/             # 95 项
│   ├── web_console/               # 150 项
│   └── agent_matrix/              # SOP 106 / BI 183 / 财务 159
│       ├── store_sop/
│       ├── supply_chain_bi/data/  # BI 黄金集、报告、对照 JSON
│       └── finance_audit/data/    # 财务单据、黄金集、制度与报告
├── web_console/                   # 统一管控与演练工作台（FastAPI 入口、离线运行时、原生前端）
├── mock_docs/                     # 虚构电商运营手册、仓储作业规范与财务制度
└── pyproject.toml / uv.lock       # Python 依赖与锁文件
```

`store_sop`、`supply_chain_bi` 等 Python 包名、API 路由、operationId、枚举取值（如 `drink_sop`、`franchise_procurement`）与数据库列名（如 `cups_sold`、`franchisee_phone`）作为对外契约保持不变，仅中文语义与演示语料面向电商场景。

## 系统架构

```mermaid
flowchart TB
    U["平台登录态 / 可信身份网关"] --> W["Dify / HiAgent 工作流结构"]
    W --> API["FastAPI / OpenAPI 3.1"]
    U --> MCP["MCP stdio Server<br/>可信身份收窄 / 只读 Tool"]
    API --> SOP["StoreSopAgent<br/>多轮槽位 / 引用核验 / 工单协同"]
    API --> BI["SupplyChainBIAgent<br/>语义目录 / AST 守卫 / 结果核验"]
    API --> FIN["FinanceAuditAgent<br/>PII 脱敏 / Decimal 规则 / 确定性路由"]
    API --> SYNC["VersionedSyncManager<br/>哈希与 ACL 短路 / v+1 影子写入 / CAS"]
    SYNC --> CHUNK["HierarchicalMarkdownChunker<br/>父块 2000 / 子块 400 / 重叠 50 字符"]
    CHUNK --> SQL[("SQLAlchemy Async<br/>kb_documents / kb_parent_chunks")]
    CHUNK --> IDX["子块索引端口<br/>向量 / 关键词 / ACL / version"]
    SOP --> RET["HybridRetrievalService<br/>ACL 前置过滤 / 并发召回 / RRF k=60 / 精排"]
    FIN --> RET
    MCP --> RET
    RET --> SQL
    RET --> IDX
    BI --> SCOPE["WarehouseScopeResolverPort"]
    BI --> DB[("SqliteReadOnlySandbox<br/>mode=ro / query_only / authorizer")]
    FIN --> LEDGER["ClaimHistoryLedgerPort"]
    FIN --> OA["HumanApprovalGatewayPort"]
    SOP --> TICKET["SessionStorePort / TicketEscalationPort"]
    EVAL["EvalRunner<br/>Semaphore / 文档去重指标 / 规则裁判"] --> RET
    EVAL --> SOP
    EVAL --> BI
    EVAL --> FIN
    EVAL --> GATE["RegressionGate<br/>ACL 红线 / 回退 / 下限 / 降级 / 错误率"]
    GATE --> REPORT["Markdown 报告与 JSON 原始记录"]
```

索引的生产目标是 Milvus / Elasticsearch；当前实测使用内存端口替身。关系存储支持 SQLAlchemy Async，离线实验使用 SQLite；仓库没有可直接启动的完整生产依赖装配。`data_platform/mcp_server` 提供 OpenAPI 工具接口；顶层 `mcp_server` 是基于官方 SDK 的 MCP stdio Server，暂无 SSE 传输。三个 YAML 是搭建参考，需要目标平台补齐节点、凭据与工具 provider。

## 核心工程特性

- **版本一致性**：规范化内容 SHA-256 与 ACL 联合短路；父块、向量、关键词并发写入影子版本；SQL CAS 发布后后台 GC。失败清理影子版本，保留已发布版本服务。
- **权限贯穿检索**：先由台账计算可见 `(doc_id, current_version)`，下推部门、角色、版本过滤；召回后复核版本，回表再核对父块的文档与版本。空权限和台账异常均关闭访问。
- **可控异步降级**：向量、关键词分支独立超时；单路失败仍可服务，双路失败返回结构化错误；精排失败回退 RRF。业务智能体按场景保留安全的拒答、模板或人工复核路径。
- **评测可追溯**：检索结果按文档去重计算 Hit@K、Recall@K、MRR；样本指纹保障可比性；系统与裁判 Token 分账；红线样本泄露、错误或缺席都会阻断发布。
- **SOP 受控循环**：仅四处模型调用点；最多两轮追问；SKU 规格数字与单位（g / ml / ℃ / 秒 / 次 等）逐字绑定目标商品所在表格行，同一父块其他 SKU 的 45g 不能冒充蛋白粉 40g；合法引用才放行；库房漏电 / 冒烟、批量资损、监管投诉等高危异常直接生成幂等 P1 工单。
- **BI 双重只读保护**：sqlglot AST 静态检查、可信范围绑定参数、文件级只读 SQLite；最多三次 SQL 尝试，致命违规立即终止；结论数字必须与结果单元格对应，否则退回模板。
- **财务确定性闭环**：Decimal 连续审批区间、税额与发票勾稽、重复占用及拆单检测；模型输入、日志、工单、响应四出口脱敏守卫；模型不能决定自动放行或推翻确定性红线。
- **工程约定**：Python 3.12、uv、FastAPI、Pydantic V2、SQLAlchemy 2.0；18 个 Protocol、124 个 Pydantic 模型；测试包含 Git/AST 契约检查。

## 已提交报告的实测基准

以下为仓库保存的原始结果，不能当作生产模型准确率、真人满意度或压力测试。红线样本不进入检索/回答质量均值；准确率与忠实度口径见各报告的实验说明。MRR 按原 Markdown 精度展示，原始 JSON 有更完整精度；P95 单位为毫秒，随运行环境变化。

| 实验 | 样本 / 红线 | K | Hit@K | Recall@K | MRR | 回答准确率 | 忠实度 | ACL 拦截 | 错误率 | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 检索评测 混合检索（RRF，关闭精排） | 38 / 10 | 3 | 100.00% | 100.00% | 0.798 | N/A | N/A | 10/10 | 0.00% | 15.31 |
| 检索评测 混合检索 + Rerank | 38 / 10 | 3 | 100.00% | 100.00% | 0.946 | N/A | N/A | 10/10 | 0.00% | 15.45 |
| 检索评测 规则端到端 | 38 / 10 | 3 | 100.00% | 100.00% | 0.946 | 100.00% | 100.00% | 10/10 | 0.00% | 15.77 |
| 电商运营 SOP 基线 | 16 / 5 | 5 | 100.00% | 100.00% | 0.894 | 100.00% | **9.09%** | 5/5 | 0.00% | 22.88 |
| 经营分析 BI 基线 | 15 / 4 | 5 | 100.0% | 100.0% | 1.000 | 100.0% | 100.0% | 4/4 | 0.0% | 142.8 |
| 财务基线 | 28 / 3 | 5 | 100.0% | 100.0% | 0.697 | 100.0% | 100.0% | 3/3 | 0.0% | 16.2 |

来源：[检索评测 报告](eval_platform/reports/latest_regression_report.md)、[电商运营 SOP 报告](eval_platform/reports/store_sop_agent_report.md)、[经营分析 BI 报告](tests/agent_matrix/supply_chain_bi/data/supply_chain_bi_report.md)、[财务预审 报告](tests/agent_matrix/finance_audit/data/finance_audit_report.md)。

检索评测中开启 Rerank 后 Hit@3 保持 100%，MRR 由 0.7976 提升至 0.9464（+0.1488），精排对比门禁通过。SOP 与 BI 的基线报告标记为“无基线”，财务为基线自比较通过。SOP 的 9.09% 忠实度来自固定规则裁判对引用标签与拒答模板的已知不适配，原始低分及 85% 下限仍保留。BI 的裁判对照中旧裁判为 0.0000，专用裁判为 1.0000。各套实验的数值幻觉、越权、隐私、规则缺失和模型故障注入均有阻断对照，不能把基线通过扩写为所有候选通过。

## 安装与复现

依赖由 [pyproject.toml](pyproject.toml) 和 [uv.lock](uv.lock) 管理。准备 Python 3.12 与 uv 后，在仓库根目录执行：

```bash
uv sync --frozen --python 3.12
uv run pytest -q
```

全量测试包含 875 项用例，覆盖数据发布、检索、评测、三个业务智能体、MCP Server 与工作台。契约测试以 `main` 分支为基线（见 [tests/contract_baseline.py](tests/contract_baseline.py)）做 AST 结构冻结：上游模块的类、签名、字段、默认值、控制流与全部 ASCII 字面量（枚举取值、SQL、正则、键名）必须与 `main` 一致，仅允许中文文案与文档字符串随业务场景调整；文案变更必须同步递增 `PROMPT_VERSION` / `catalog_version`，安全关键词集合只能扩充、不能删减。因此需要完整 Git 仓库且本地存在 `main` 分支。测试报告写入 pytest 临时目录，不覆盖已提交的评测报告。

如需重新生成检索、SOP 和经营分析报告，可在独立克隆中运行以下脚本；默认输出会更新克隆内的报告与 JSON：

```bash
uv run python -m tests.eval_platform.run_ecommerce
uv run python -m tests.agent_matrix.store_sop.run_e2e
uv run python -m tests.agent_matrix.supply_chain_bi.run_e2e
```

财务评测包含基线、规则缺失、隐私泄露、越权注入、模型故障和金额幻觉对照。以下命令将报告保存在指定临时目录的测试子目录中，文件名为 `finance_audit_e2e_report.md`：

```bash
uv run pytest -v tests/agent_matrix/finance_audit/test_eval_e2e.py --basetemp=/tmp/enterprise-finance-eval
```

先运行全量测试，再生成报告，避免报告变化影响上游模块的无侵入性检查。离线实验不需要外部模型密钥或 Milvus / Elasticsearch 服务；报告延迟和时间戳随运行环境变化。

## 可视化工作台

`web_console/` 把上述模块装配成一个本地可运行的中后台界面，无需外部模型密钥、Milvus / Elasticsearch 或前端构建链，页面资源全部来自本地，断网可用：

```bash
uv sync --frozen --python 3.12
uv run python -m web_console
```

启动后访问 <http://127.0.0.1:8000/>。左侧切换模块，顶部显示运行环境，右侧“引擎透视”抽屉展示每次调用的内部状态。每个模块顶部提供 3 个一键用例。

| 模块 | 演示内容 |
|---|---|
| 电商运营 SOP 与工单协同 | 同系列 SKU 规格参数查询、热敏打单机多轮排障、排障超限自动转人工工单；员工身份切换、会话重置；展示溯源引用、`ExtractedSlots`、`LoopStep` 轨迹、`DegradedReason` 与 `trace_id`。输入“订单管理系统出现批量资损”可演示高危异常直派 P1 工单 |
| 经营分析与进销存预警 | 自然语言生成 SQL、sqlglot 放行或拦截徽章、只读沙箱结果表、缺货 / 临期预警；可演示多语句注入拦截 |
| 财务报销与采购合同审批 | 报销单或仓储物流 / 商品采购合同文本预审，脱敏前后对比、Decimal 规则命中明细、人工审批工单回执 |
| 知识数据中台检索器 | 按部门与角色检索，展示召回分值、父子块对照与跨部门过滤效果 |
| 评测中台与门禁看板 | 读取已提交的评测报告，展示各智能体指标、失败归因与回归门禁状态 |

### 在线模型模式

未配置 API Key 时工作台保持离线确定性模式。需要接入真实模型时有两种方式，网关须兼容标准 `/chat/completions` 协议：

- 环境变量或 `.env`：复制 `.env.example` 为 `.env`，填写 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_TIMEOUT_SECONDS`，系统环境变量优先于 `.env`。
- 页面配置：点击顶部的模式或模型胶囊打开配置抽屉，可测试连通性并保存生效。页面输入的 Key 只保存在服务进程内存中，重启或清空后回到离线模式（已配置环境变量时，重启会按环境变量重新进入在线模式）。

在线模式只替换三大智能体的模型端口，检索、SQL 守卫与沙箱、财务规则和 PII 脱敏仍在本地执行。用户问题、制度片段、语义字典、查询结果和脱敏后的单据会发送到所配置的网关。顶部“外部调用”统计每一次真实发出的请求，包括失败请求和连通性测试。

运行时真实调用 `VersionedSyncManager`、`HybridRetrievalService`、`SqlStaticGuard`、`SqliteReadOnlySandbox`、`DeterministicFinanceRuleEngine`、`PiiRedactor` 与三大智能体，四个应用工厂挂载在 `/gateway/` 下，由演示网关清洗外部身份头并按所选人员注入可信身份。语言模型位置默认使用 `web_console/adapters.py` 中的确定性适配器，检索使用字符二元组特征的内存索引，经营数据为启动时写入临时目录的虚构样本，因此界面结果用于演示链路和安全控制，不代表生产模型效果。

## MCP Server

```bash
uv run python -m mcp_server
```

stdio 模式下 stdout 只输出 MCP JSON-RPC 报文，日志写 stderr。可信身份由 `EAP_MCP_USER_ID`、`EAP_MCP_DEPARTMENT`、`EAP_MCP_ROLES` 注入，未设置时为最低权限的电商运营专员演示身份；工具入参的部门与角色只能在可信身份范围内收窄，随后仍执行台账 ACL 前置过滤。提供 `search_enterprise_knowledge`、`list_supply_chain_metrics` 两个只读 Tool 与 `sop_citation_qa_prompt` 引用问答 Prompt。

## OpenAPI 与平台对接

四个应用工厂可在不注入运行时依赖的情况下导出 OpenAPI 3.1.0，共 9 个业务路由。以财务工具为例：

```bash
uv run --frozen python -c 'import json; from agent_matrix.finance_audit.api import create_app; print(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2))' > /tmp/finance-audit.openapi.json
```

其余工厂：`data_platform.mcp_server.app.create_app`、`agent_matrix.store_sop.api.create_app`、`agent_matrix.supply_chain_bi.api.create_app`。业务请求需要注入各自模型、台账、检索、权限或工单端口；空装配仅能导出 Schema，不能提供完整业务服务。

Agent API 从 `X-User-Id`、`X-Dept-Id`、`X-User-Roles` 构造可信身份并覆盖 body；网关负责认证并清洗外部同名头。知识工具的身份仍来自 `SearchRequest.user_context`，生产接入需可信网关处理。错误统一为 `ToolErrorResponse`，包含错误码、修正提示和可重试标记。财务 `AUTO_APPROVE` 仅表示预审通过并进入常规审批，不表示付款。
