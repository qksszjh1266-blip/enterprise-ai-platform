# 连锁餐饮企业 AI 平台

面向万店级连锁餐饮集团的企业级 AI 平台工程：**知识数据中台 + 智能体评测中台 + 三大业务智能体矩阵**。以门店 SOP 问答、供应链经营分析、财务费用与合同预审为场景，把权限、证据、确定性规则、故障降级和回归门禁落实为可执行代码。

当前仓库提供可离线复现的工程实现与验收实验。“万店级”是业务定位；已有报告使用合成语料、内存检索替身和确定性模型，并未验证万店并发容量或生产大模型效果。架构、复现方法与评测边界见下文；各模块接口约定由模型、端口和契约测试共同维护。

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
│   ├── golden_datasets/           # 餐饮 32 条、SOP 15 条黄金集
│   ├── reports/                   # 检索与门店 SOP 报告与逐样本 JSON
│   ├── evaluators.py              # 文档级检索指标与 ACL 检测
│   ├── runner.py                  # EvalRunner、RegressionGate
│   └── report_generator.py        # Markdown 运营报告
├── agent_matrix/
│   ├── store_sop/                 # 多轮问答、引用核验、报修
│   ├── supply_chain_bi/           # 语义目录、AST、只读 SQL、预警
│   ├── finance_audit/             # Decimal 勾稽、脱敏、引用、审批路由
│   └── workflows/                # 三份 Dify / HiAgent 编排结构说明
├── tests/                        # 646 项参数化展开后的 pytest 测试
│   ├── parser_chunker/            # 15 项
│   ├── storage_sync/              # 28 项
│   ├── retrieval_engine/          # 44 项
│   ├── mcp_server/                # 16 项
│   ├── eval_platform/             # 95 项
│   └── agent_matrix/              # SOP 106 / BI 183 / 财务 159
│       ├── store_sop/
│       ├── supply_chain_bi/data/  # BI 黄金集、报告、对照 JSON
│       └── finance_audit/data/    # 财务单据、黄金集、制度与报告
├── mock_docs/                     # 虚构培训制度与演示 SOP
└── pyproject.toml / uv.lock       # Python 依赖与锁文件
```

## 系统架构

```mermaid
flowchart TB
    U["平台登录态 / 可信身份网关"] --> W["Dify / HiAgent 工作流结构"]
    W --> API["FastAPI / OpenAPI 3.1"]
    API --> SOP["StoreSopAgent<br/>多轮槽位 / 引用核验 / 报修"]
    API --> BI["SupplyChainBIAgent<br/>语义目录 / AST 守卫 / 结果核验"]
    API --> FIN["FinanceAuditAgent<br/>PII 脱敏 / Decimal 规则 / 确定性路由"]
    API --> SYNC["VersionedSyncManager<br/>哈希与 ACL 短路 / v+1 影子写入 / CAS"]
    SYNC --> CHUNK["HierarchicalMarkdownChunker<br/>父块 2000 / 子块 400 / 重叠 50 字符"]
    CHUNK --> SQL[("SQLAlchemy Async<br/>kb_documents / kb_parent_chunks")]
    CHUNK --> IDX["子块索引端口<br/>向量 / 关键词 / ACL / version"]
    SOP --> RET["HybridRetrievalService<br/>ACL 前置过滤 / 并发召回 / RRF k=60 / 精排"]
    FIN --> RET
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

索引的生产目标是 Milvus / Elasticsearch；当前实测使用内存端口替身。关系存储支持 SQLAlchemy Async，离线实验使用 SQLite；仓库没有可直接启动的完整生产依赖装配。`mcp_server` 当前实现的是 OpenAPI 工具接口，尚无原生 MCP stdio/SSE 服务。三个 YAML 是搭建参考，需要目标平台补齐节点、凭据与工具 provider。

## 核心工程特性

- **版本一致性**：规范化内容 SHA-256 与 ACL 联合短路；父块、向量、关键词并发写入影子版本；SQL CAS 发布后后台 GC。失败清理影子版本，保留已发布版本服务。
- **权限贯穿检索**：先由台账计算可见 `(doc_id, current_version)`，下推部门、角色、版本过滤；召回后复核版本，回表再核对父块的文档与版本。空权限和台账异常均关闭访问。
- **可控异步降级**：向量、关键词分支独立超时；单路失败仍可服务，双路失败返回结构化错误；精排失败回退 RRF。业务智能体按场景保留安全的拒答、模板或人工复核路径。
- **评测可追溯**：检索结果按文档去重计算 Hit@K、Recall@K、MRR；样本指纹保障可比性；系统与裁判 Token 分账；红线样本泄露、错误或缺席都会阻断发布。
- **SOP 受控循环**：仅四处模型调用点；最多两轮追问；饮品数字与单位逐字绑定目标配方；合法引用才放行，安全隐患可直接生成幂等报修工单。
- **BI 双重只读保护**：sqlglot AST 静态检查、可信范围绑定参数、文件级只读 SQLite；最多三次 SQL 尝试，致命违规立即终止；结论数字必须与结果单元格对应，否则退回模板。
- **财务确定性闭环**：Decimal 连续审批区间、税额与发票勾稽、重复占用及拆单检测；模型输入、日志、工单、响应四出口脱敏守卫；模型不能决定自动放行或推翻确定性红线。
- **工程约定**：Python 3.12、uv、FastAPI、Pydantic V2、SQLAlchemy 2.0；18 个 Protocol、124 个 Pydantic 模型；测试包含 Git/AST 契约检查。

## 已提交报告的实测基准

以下为仓库保存的原始结果，不能当作生产模型准确率、真人满意度或压力测试。红线样本不进入检索/回答质量均值；准确率与忠实度口径见各报告的实验说明。MRR 按原 Markdown 精度展示，原始 JSON 有更完整精度；P95 单位为毫秒。

| 实验 | 样本 / 红线 | K | Hit@K | Recall@K | MRR | 回答准确率 | 忠实度 | ACL 拦截 | 错误率 | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 检索评测 关闭精排 | 32 / 8 | 3 | 100.00% | 100.00% | 0.722 | N/A | N/A | 8/8 | 0.00% | 14.07 |
| 检索评测 开启精排 | 32 / 8 | 3 | 100.00% | 100.00% | 0.958 | N/A | N/A | 8/8 | 0.00% | 14.64 |
| 检索评测 规则端到端 | 32 / 8 | 3 | 100.00% | 100.00% | 0.958 | 100.00% | 100.00% | 8/8 | 0.00% | 15.71 |
| 门店 SOP 基线 | 15 / 5 | 5 | 100.00% | 100.00% | 0.883 | 100.00% | **10.00%** | 5/5 | 0.00% | 23.41 |
| 供应链 BI 基线 | 15 / 4 | 5 | 100.0% | 100.0% | 1.000 | 100.0% | 100.0% | 4/4 | 0.0% | 166.8 |
| 财务基线 | 28 / 3 | 5 | 100.0% | 100.0% | 0.697 | 100.0% | 100.0% | 3/3 | 0.0% | 16.2 |

来源：[检索评测 报告](eval_platform/reports/latest_regression_report.md)、[门店 SOP 报告](eval_platform/reports/store_sop_agent_report.md)、[供应链 BI 报告](tests/agent_matrix/supply_chain_bi/data/supply_chain_bi_report.md)、[财务预审 报告](tests/agent_matrix/finance_audit/data/finance_audit_report.md)。

检索评测 精排对比门禁通过；SOP 与 BI 的基线报告标记为“无基线”，财务为基线自比较通过。SOP 的 10% 忠实度来自固定规则裁判对引用标签与拒答模板的已知不适配，原始低分及 85% 下限仍保留。BI 的裁判对照中旧裁判为 0.0000，专用裁判为 1.0000。各套实验的数值幻觉、越权、隐私、规则缺失和模型故障注入均有阻断对照，不能把基线通过扩写为所有候选通过。

## 安装与复现

依赖由 [pyproject.toml](pyproject.toml) 和 [uv.lock](uv.lock) 管理。准备 Python 3.12 与 uv 后，在仓库根目录执行：

```bash
uv sync --frozen --python 3.12
uv run pytest -v
```

全量测试包含 646 项用例，覆盖数据发布、检索、评测与三个业务智能体。契约测试使用 `main` / `HEAD` 检查公共接口和上游模块，因此需要完整 Git 仓库，并从已提交的干净工作区运行。测试报告写入 pytest 临时目录，不覆盖已提交的评测报告。

如需重新生成检索、SOP 和供应链报告，可在独立克隆中运行以下脚本；默认输出会更新克隆内的报告与 JSON：

```bash
uv run python -m tests.eval_platform.run_catering
uv run python -m tests.agent_matrix.store_sop.run_e2e
uv run python -m tests.agent_matrix.supply_chain_bi.run_e2e
```

财务评测包含基线、规则缺失、隐私泄露、越权注入、模型故障和金额幻觉对照。以下命令将报告保存在指定临时目录的测试子目录中，文件名为 `finance_audit_e2e_report.md`：

```bash
uv run pytest -v tests/agent_matrix/finance_audit/test_eval_e2e.py --basetemp=/tmp/enterprise-finance-eval
```

先运行全量测试，再生成报告，避免报告变化影响上游模块的无侵入性检查。离线实验不需要外部模型密钥或 Milvus / Elasticsearch 服务；报告延迟和时间戳随运行环境变化。

## OpenAPI 与平台对接

四个应用工厂可在不注入运行时依赖的情况下导出 OpenAPI 3.1.0，共 9 个业务路由。以财务工具为例：

```bash
uv run --frozen python -c 'import json; from agent_matrix.finance_audit.api import create_app; print(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2))' > /tmp/finance-audit.openapi.json
```

其余工厂：`data_platform.mcp_server.app.create_app`、`agent_matrix.store_sop.api.create_app`、`agent_matrix.supply_chain_bi.api.create_app`。业务请求需要注入各自模型、台账、检索、权限或工单端口；空装配仅能导出 Schema，不能提供完整业务服务。

Agent API 从 `X-User-Id`、`X-Dept-Id`、`X-User-Roles` 构造可信身份并覆盖 body；网关负责认证并清洗外部同名头。知识工具的身份仍来自 `SearchRequest.user_context`，生产接入需可信网关处理。错误统一为 `ToolErrorResponse`，包含错误码、修正提示和可重试标记。财务 `AUTO_APPROVE` 仅表示预审通过并进入常规审批，不表示付款。
