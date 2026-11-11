"""供应链 BI 智能体数据模型与传输契约。

本模块仅包含 Pydantic V2 模型、枚举与常量，不含任何 I/O：

语义层数据字典
- ColumnSpec / TableSchemaSpec        允许查询的物理表、列白名单、敏感列黑名单、行级权限键
- MetricDefinition                    标准业务指标（商品货损率 / 安全库存缺口 / 效期周转天数 …）及计算口径
- AlertQueryTemplate                  目录内置的确定性预警查询（非 LLM 生成）
- SemanticCatalogSpec                 整个语义目录（表 + 指标 + 全局黑名单 + 系统元数据表模式）
- SemanticContext                     送入 SqlGeneratorModelPort 的脱敏语义上下文（不含敏感列名）

行级权限
- WarehouseScopeFilter                可信身份解析出的大区 / 授权仓库范围（绑定参数来源，绝不来自 LLM）

SQL 守卫与执行
- SqlViolation / SqlValidationResult  静态守卫结论（含规范化 SQL、自动 LIMIT、error_hint）
- ResultColumn / SqlExecutionResult   只读沙箱执行结果（截断标记、行数、列与行、耗时，或 error_hint）
- SqlAttempt                          Text-to-SQL 自纠错循环的一次尝试

模型传输（ports.py 使用）
- SqlModelResult[T] / AnalysisIntent / SqlCandidate / KeyFigure / AnalysisNarrativeDraft

业务输出
- InventoryAlert                      安全库存跌破 / 临期积压时的补货 / 调拨预警（规则引擎生成，非 LLM）
- ResultTable                         响应中的结果摘要表
- SupplyChainQueryRequest / SupplyChainQueryPayload / SupplyChainQueryResponse
- SupplyChainAgentConfig / AlertRuleConfig

关键约定（实现与测试必须遵守）：
1. 行级权限值只经由绑定参数注入：候选 SQL 必须使用 SCOPE_REGION_PARAM / SCOPE_WAREHOUSES_PARAM 占位符，
   参数值由 WarehouseScopeFilter.bind_params() 产出；LLM 输出 schema 一律 extra="forbid" 且不含任何身份 / 范围字段。
2. 敏感列（如 supplier_cost_price）永不出现在 SemanticContext、SQL、结果、结论与日志中。
3. 比例类数值在结果中保持 0~1 原值，展示层再格式化为百分比；数值一致性核验按 MetricDefinition.decimal_places 容差比较。
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_platform.retrieval_engine.models import UserContext
from eval_platform.models import TokenUsage

T = TypeVar("T", bound=BaseModel)

ScalarValue = str | int | float | bool | None
"""结果单元格与绑定参数允许的标量类型（date / Decimal 由沙箱适配器转为 ISO 字符串 / float）。"""

SCOPE_REGION_PARAM = "scope_region_id"
"""候选 SQL 中大区过滤的命名绑定参数：`<alias>.region_id = :scope_region_id`。"""

SCOPE_WAREHOUSES_PARAM = "scope_warehouse_ids"
"""候选 SQL 中仓库过滤的命名绑定参数（expanding）：`<alias>.warehouse_id IN :scope_warehouse_ids`。"""

AS_OF_DATE_PARAM = "as_of_date"
"""分析基准日绑定参数（ISO 日期字符串）；SQL 中禁止使用 CURRENT_DATE / date('now') 等非确定性函数。"""

RESERVED_BIND_PARAMS: frozenset[str] = frozenset({SCOPE_REGION_PARAM, SCOPE_WAREHOUSES_PARAM, AS_OF_DATE_PARAM})
"""候选 SQL 只允许出现这三个命名参数；出现其他参数视为 UNKNOWN_BIND_PARAM。"""

_IDENT = r"^[a-z][a-z0-9_]{0,62}$"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class SqlDialect(str, enum.Enum):
    """沙箱方言（守卫解析与 LLM 生成均以此为准）。"""

    SQLITE = "sqlite"  # 测试 / 模拟库
    POSTGRESQL = "postgresql"  # 生产只读从库


class ColumnDataType(str, enum.Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    BOOLEAN = "boolean"


class MetricUnit(str, enum.Enum):
    RATIO = "ratio"  # 0~1，展示为百分比
    QUANTITY = "quantity"  # 物料基本单位（kg / 箱 / 袋，见 MetricDefinition.quantity_unit_column）
    DAYS = "days"
    CUPS = "cups"
    CNY = "cny"  # 仅限店铺零售营收；采购成本类金额一律不可查


class ScopeLevel(str, enum.Enum):
    """行级权限范围层级。"""

    WAREHOUSE = "warehouse"  # 单仓 / 多仓主管：region_id + 授权仓库列表
    REGION = "region"  # 大区经理：region_id 下全部仓库
    NATIONAL = "national"  # 总部供应链分析：全国（敏感列黑名单仍然生效）


class AnalysisIntentType(str, enum.Enum):
    METRIC_QUERY = "metric_query"  # 指标查询 / 对比 / 排名 / 趋势
    ALERT_SCAN = "alert_scan"  # "哪些商品要补货 / 哪些批次快过期"
    SENSITIVE_REQUEST = "sensitive_request"  # 询问采购底价、供应商合同价、利润底表等
    OUT_OF_SCOPE = "out_of_scope"  # 非供应链分析（电商运营 SOP、报销、闲聊）


class SqlViolationCode(str, enum.Enum):
    """静态守卫违规码。fatal 与否见 FATAL_VIOLATION_CODES。"""

    PARSE_ERROR = "parse_error"
    EMPTY_STATEMENT = "empty_statement"
    MULTI_STATEMENT = "multi_statement"
    NON_SELECT_STATEMENT = "non_select_statement"  # 非 SELECT / WITH（含 PRAGMA / EXPLAIN / ATTACH / SET …）
    WRITE_OPERATION = "write_operation"  # INSERT / UPDATE / DELETE / MERGE / DDL / SELECT INTO / 可写 CTE
    SYSTEM_CATALOG_ACCESS = "system_catalog_access"  # sqlite_master / information_schema / pg_catalog …
    FORBIDDEN_TABLE = "forbidden_table"  # 显式禁止的表（供应商合同价、财务结算底表）
    TABLE_NOT_WHITELISTED = "table_not_whitelisted"
    COLUMN_NOT_WHITELISTED = "column_not_whitelisted"
    SENSITIVE_COLUMN = "sensitive_column"
    SELECT_STAR_FORBIDDEN = "select_star_forbidden"  # 含 t.*；防止敏感列经展开泄露
    FORBIDDEN_FUNCTION = "forbidden_function"  # load_extension / pg_sleep / randomblob / 非确定性时间函数 …
    UNKNOWN_BIND_PARAM = "unknown_bind_param"
    SCOPE_PREDICATE_MISSING = "scope_predicate_missing"  # 受控表缺少顶层 AND 合取的范围谓词
    SCOPE_LITERAL_OUT_OF_SCOPE = "scope_literal_out_of_scope"  # 收窄用的 region_id / warehouse_id 字面量不在授权范围内
    SCOPE_PREDICATE_WEAKENED = "scope_predicate_weakened"  # 范围谓词位于 OR / NOT / CASE 分支内


FATAL_VIOLATION_CODES: frozenset[SqlViolationCode] = frozenset(
    {
        SqlViolationCode.WRITE_OPERATION,
        SqlViolationCode.SYSTEM_CATALOG_ACCESS,
        SqlViolationCode.FORBIDDEN_TABLE,
        SqlViolationCode.SENSITIVE_COLUMN,
        SqlViolationCode.FORBIDDEN_FUNCTION,
    }
)
"""安全类违规：立即终止循环、不执行、不重试、不把违规片段回灌模型（REFUSED(POLICY_VIOLATION)）。
其余违规为可纠错类：error_hint 回灌 SqlGeneratorModelPort.repair_sql。"""


class SqlExecutionStatus(str, enum.Enum):
    SUCCESS = "success"
    SYNTAX_ERROR = "syntax_error"  # 引擎侧解析失败（守卫放行但方言不兼容）
    UNKNOWN_COLUMN = "unknown_column"
    UNKNOWN_TABLE = "unknown_table"
    TYPE_ERROR = "type_error"
    TIMEOUT = "timeout"
    READ_ONLY_VIOLATION = "read_only_violation"  # 底层只读保护触发（守卫失守的纵深证据，需告警）
    RUNTIME_ERROR = "runtime_error"


RETRYABLE_EXECUTION_STATUSES: frozenset[SqlExecutionStatus] = frozenset(
    {
        SqlExecutionStatus.SYNTAX_ERROR,
        SqlExecutionStatus.UNKNOWN_COLUMN,
        SqlExecutionStatus.UNKNOWN_TABLE,
        SqlExecutionStatus.TYPE_ERROR,
        SqlExecutionStatus.TIMEOUT,
    }
)
"""可回灌自纠错的执行失败；READ_ONLY_VIOLATION 视为安全事件，与 FATAL 守卫违规同样立即终止。"""


class AttemptOutcome(str, enum.Enum):
    GUARD_REJECTED = "guard_rejected"
    GUARD_FATAL = "guard_fatal"
    EXECUTION_FAILED = "execution_failed"
    SUCCEEDED = "succeeded"


class AlertType(str, enum.Enum):
    SAFETY_STOCK_BREACH = "safety_stock_breach"  # 可用库存跌破安全库存
    NEAR_EXPIRY_OVERSTOCK = "near_expiry_overstock"  # 批次预计消化天数超过剩余效期


class AlertSeverity(str, enum.Enum):
    P1 = "p1"  # 预计 ≤ 1 天断货 / 剩余效期 ≤ 3 天
    P2 = "p2"
    P3 = "p3"


class AlertAction(str, enum.Enum):
    REPLENISH = "replenish"  # 向上游中心仓 / 供应商补货
    TRANSFER_IN = "transfer_in"  # 从授权范围内富余仓调入
    TRANSFER_OUT = "transfer_out"  # 临期批次调往授权范围内高消耗仓
    PRIORITIZE_CONSUMPTION = "prioritize_consumption"  # 先进先出 / 订单优先履约
    INSPECT_AND_DISPOSE = "inspect_and_dispose"  # 剩余效期不足以消化，需质检与报损审批


class StopReason(str, enum.Enum):
    ANSWERED = "answered"
    CLARIFICATION_REQUESTED = "clarification_requested"  # 指标歧义 / 置信度不足
    REFUSED = "refused"
    SQL_RETRY_EXHAUSTED = "sql_retry_exhausted"  # N 次仍未得到合规且可执行的 SQL
    DATA_SOURCE_UNAVAILABLE = "data_source_unavailable"  # 沙箱基础设施不可用（非 SQL 错误）


class RefusalReason(str, enum.Enum):
    OUT_OF_SCOPE = "out_of_scope"
    SENSITIVE_FIELD = "sensitive_field"  # 意图阶段识别出采购底价等敏感诉求
    SCOPE_VIOLATION = "scope_violation"  # 明确要求授权范围外的大区 / 仓库 / 全国数据
    POLICY_VIOLATION = "policy_violation"  # 守卫 FATAL 或沙箱 READ_ONLY_VIOLATION


class NarrativeSource(str, enum.Enum):
    LLM_VERIFIED = "llm_verified"  # 模型结论通过数值一致性核验
    TEMPLATE_FALLBACK = "template_fallback"  # 核验失败 / 模型摘要失败，使用确定性模板结论
    NOT_APPLICABLE = "not_applicable"  # 追问 / 拒答 / 失败


class LoopStage(str, enum.Enum):
    """stage_latency_ms 的固定键。"""

    SCOPE = "scope"
    INTENT = "intent"
    SEMANTIC = "semantic"
    GENERATE = "generate"
    GUARD = "guard"
    EXECUTE = "execute"
    REPAIR = "repair"
    ALERT_SCAN = "alert_scan"
    VERIFY = "verify"
    SUMMARIZE = "summarize"


# ---------------------------------------------------------------------------
# 语义层数据字典
# ---------------------------------------------------------------------------


class ColumnSpec(BaseModel):
    """物理列定义（仅白名单列出现在此处；敏感列只登记名称于 TableSchemaSpec.sensitive_columns）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., pattern=_IDENT)
    data_type: ColumnDataType
    description: str = Field(..., min_length=1, max_length=200, description="业务含义，进入 SemanticContext")
    is_dimension: bool = Field(default=False, description="可用于 GROUP BY 的维度列")
    unit: str | None = Field(default=None, max_length=16, description="数量列计量单位，如 kg / 箱")


class TableSchemaSpec(BaseModel):
    """允许查询的物理表规格。

    - columns 即列白名单；sensitive_columns 为该表的列黑名单（与全局黑名单取并集）。
    - requires_scope_filter=True 的表在任何 FROM / JOIN / 子查询 / CTE 中出现，都必须带顶层范围谓词。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_name: str = Field(..., pattern=_IDENT)
    display_name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=500)
    grain: str = Field(..., min_length=1, max_length=200, description="一行代表什么，如 仓库×物料×日")
    columns: list[ColumnSpec] = Field(..., min_length=1)
    sensitive_columns: list[str] = Field(default_factory=list, description="物理存在但禁止查询的列")
    time_column: str | None = Field(default=None, description="分区 / 时间过滤列")
    region_column: str | None = Field(default="region_id", description="行级大区键；None 表示维表无大区")
    warehouse_column: str | None = Field(default="warehouse_id", description="行级仓库键；None 表示无仓库维度")
    requires_scope_filter: bool = True
    max_lookback_days: int = Field(default=180, ge=1, le=730)

    @model_validator(mode="after")
    def _check_columns(self) -> TableSchemaSpec:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.table_name}: 列名重复")
        if overlap := set(names) & set(self.sensitive_columns):
            raise ValueError(f"{self.table_name}: 敏感列不得同时出现在白名单: {sorted(overlap)}")
        for key in (self.time_column, self.region_column, self.warehouse_column):
            if key is not None and key not in names:
                raise ValueError(f"{self.table_name}: 键列 {key} 不在白名单")
        if self.requires_scope_filter and self.region_column is None:
            raise ValueError(f"{self.table_name}: 受控表必须声明 region_column")
        return self

    @property
    def allowed_columns(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)


class MetricDefinition(BaseModel):
    """标准业务指标（语义层唯一口径来源）。

    formula_sql 为参考表达式片段（非完整 SQL），供模型复用口径；agent 不做字符串拼接执行。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_id: str = Field(..., pattern=_IDENT)
    display_name: str = Field(..., min_length=1, max_length=32, description="标准业务指标名，如 商品货损率")
    aliases: list[str] = Field(default_factory=list, description="同义词，用于语义匹配，如 报损率 / 损耗占比")
    calculation_caliber: str = Field(..., min_length=1, max_length=1000, description="标准计算口径描述（中文）")
    formula_sql: str = Field(..., min_length=1, max_length=1000, description="参考 SQL 表达式")
    unit: MetricUnit
    decimal_places: int = Field(default=2, ge=0, le=6, description="展示精度；数值核验容差 = 0.5 × 10^-decimal_places")
    source_tables: list[str] = Field(..., min_length=1)
    required_columns: dict[str, list[str]] = Field(..., description="table_name -> 口径依赖列")
    dimensions: list[str] = Field(default_factory=list, description="推荐下钻维度列")
    default_lookback_days: int = Field(default=7, ge=1, le=365)
    higher_is_better: bool | None = None
    owner_dept: str = Field(default="supply_chain", max_length=64)

    @model_validator(mode="after")
    def _check_dependencies(self) -> MetricDefinition:
        if extra := set(self.required_columns) - set(self.source_tables):
            raise ValueError(f"{self.metric_id}: required_columns 引用了非 source_tables 的表 {sorted(extra)}")
        return self


class AlertQueryTemplate(BaseModel):
    """目录内置的预警扫描 SQL（同样必须通过 SqlStaticGuard，且只使用 RESERVED_BIND_PARAMS）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str = Field(..., pattern=_IDENT)
    alert_type: AlertType
    metric_id: str = Field(..., pattern=_IDENT)
    sql: str = Field(..., min_length=1)
    expected_columns: list[str] = Field(..., min_length=1, description="规则引擎依赖的结果列，按顺序")


class SemanticCatalogSpec(BaseModel):
    """电商供应链语义目录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: int = Field(..., ge=1, description="修改任何表 / 指标 / 模板必须递增")
    dialect: SqlDialect
    tables: list[TableSchemaSpec] = Field(..., min_length=1)
    metrics: list[MetricDefinition] = Field(..., min_length=1)
    alert_templates: list[AlertQueryTemplate] = Field(default_factory=list)
    global_sensitive_columns: list[str] = Field(..., min_length=1, description="任意表中同名列一律拦截")
    forbidden_tables: list[str] = Field(default_factory=list, description="物理存在但禁止任何访问的表")
    system_catalog_patterns: list[str] = Field(..., min_length=1, description="小写前缀 / 名称，如 sqlite_ / pg_ / information_schema")
    forbidden_functions: list[str] = Field(..., min_length=1, description="小写函数名")

    @model_validator(mode="after")
    def _check_catalog(self) -> SemanticCatalogSpec:
        tables = {t.table_name: t for t in self.tables}
        if len(tables) != len(self.tables):
            raise ValueError("表名重复")
        if overlap := set(tables) & set(self.forbidden_tables):
            raise ValueError(f"禁止表不得出现在白名单: {sorted(overlap)}")
        for t in self.tables:
            if leak := t.allowed_columns & set(self.global_sensitive_columns):
                raise ValueError(f"{t.table_name}: 全局敏感列出现在白名单 {sorted(leak)}")
        metric_ids = [m.metric_id for m in self.metrics]
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("指标 ID 重复")
        for m in self.metrics:
            for table, cols in m.required_columns.items():
                if table not in tables:
                    raise ValueError(f"{m.metric_id}: 未登记表 {table}")
                if missing := set(cols) - tables[table].allowed_columns:
                    raise ValueError(f"{m.metric_id}: {table} 缺少白名单列 {sorted(missing)}")
            if missing_src := set(m.source_tables) - set(tables):
                raise ValueError(f"{m.metric_id}: 未登记表 {sorted(missing_src)}")
        for tpl in self.alert_templates:
            if tpl.metric_id not in metric_ids:
                raise ValueError(f"{tpl.template_id}: 未登记指标 {tpl.metric_id}")
        return self


class SemanticContext(BaseModel):
    """送入模型的脱敏语义上下文（由 SemanticLayer.build_context 生成）。

    不含：敏感列名、forbidden_tables、system_catalog_patterns、任何范围参数的取值。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: int = Field(..., ge=1)
    dialect: SqlDialect
    metrics: list[MetricDefinition] = Field(default_factory=list)
    tables: list[TableSchemaSpec] = Field(..., min_length=1, description="sensitive_columns 已清空的副本")
    scope_level: ScopeLevel
    scope_instructions: str = Field(..., description="确定性生成：如何书写 :scope_region_id / :scope_warehouse_ids 谓词")
    as_of_date: date
    max_rows: int = Field(..., ge=1)


# ---------------------------------------------------------------------------
# 行级权限
# ---------------------------------------------------------------------------


class WarehouseScopeFilter(BaseModel):
    """可信行级权限范围（WarehouseScopeResolverPort 由组织权限系统解析；不得由 LLM 或请求体构造）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=128)
    scope_level: ScopeLevel
    region_id: str | None = Field(default=None, min_length=1, max_length=64)
    authorized_warehouse_ids: list[str] = Field(default_factory=list, description="WAREHOUSE 级必填；升序去重")
    policy_version: str = Field(default="v1", max_length=32, description="权限策略版本，进入审计")
    resolved_at: datetime

    @model_validator(mode="after")
    def _check_level(self) -> WarehouseScopeFilter:
        if self.scope_level is ScopeLevel.WAREHOUSE:
            if self.region_id is None or not self.authorized_warehouse_ids:
                raise ValueError("WAREHOUSE 级范围必须同时给出 region_id 与至少 1 个授权仓库")
        elif self.scope_level is ScopeLevel.REGION:
            if self.region_id is None or self.authorized_warehouse_ids:
                raise ValueError("REGION 级范围必须给出 region_id，且不列举仓库")
        elif self.region_id is not None or self.authorized_warehouse_ids:
            raise ValueError("NATIONAL 级范围不得携带 region_id / 仓库列表")
        if self.authorized_warehouse_ids != sorted(set(self.authorized_warehouse_ids)):
            raise ValueError("authorized_warehouse_ids 必须升序去重")
        if self.resolved_at.tzinfo is None:
            raise ValueError("resolved_at 必须为 UTC aware")
        return self

    def bind_params(self, *, as_of_date: date) -> dict[str, ScalarValue | list[str]]:
        """产出沙箱绑定参数：WAREHOUSE → region + warehouses；REGION → region；NATIONAL → 仅 as_of_date。"""
        params = {AS_OF_DATE_PARAM: as_of_date.isoformat()}
        if self.scope_level is not ScopeLevel.NATIONAL:
            params[SCOPE_REGION_PARAM] = self.region_id
        if self.scope_level is ScopeLevel.WAREHOUSE:
            params[SCOPE_WAREHOUSES_PARAM] = list(self.authorized_warehouse_ids)
        return params


# ---------------------------------------------------------------------------
# SQL 守卫与执行
# ---------------------------------------------------------------------------


class SqlViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: SqlViolationCode
    message: str = Field(..., max_length=500, description="面向模型的中文说明；FATAL 违规不得包含敏感列名")
    table: str | None = None
    column: str | None = Field(default=None, description="FATAL 违规时为 None，避免回显敏感列名")


class SqlValidationResult(BaseModel):
    """SqlStaticGuard.validate 结论。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_valid: bool
    fatal: bool = Field(default=False, description="存在任一 FATAL_VIOLATION_CODES 违规")
    violations: list[SqlViolation] = Field(default_factory=list)
    original_sql: str
    normalized_sql: str | None = Field(default=None, description="通过时：去注释、单语句、已追加 / 收紧 LIMIT 的可执行 SQL")
    referenced_tables: list[str] = Field(default_factory=list, description="升序去重（含 CTE 内部引用的物理表）")
    referenced_columns: dict[str, list[str]] = Field(default_factory=dict, description="table -> 升序去重列")
    bind_params_used: list[str] = Field(default_factory=list)
    limit_applied: int | None = Field(default=None, description="最终生效的 LIMIT")
    limit_rewritten: bool = Field(default=False, description="原 SQL 无 LIMIT 或 LIMIT 超上限而被改写")
    error_hint: str | None = Field(default=None, description="不通过时的结构化修正提示，回灌 repair_sql")

    @model_validator(mode="after")
    def _check_consistency(self) -> SqlValidationResult:
        if self.is_valid != (not self.violations):
            raise ValueError("is_valid 必须等价于 violations 为空")
        if self.fatal != any(v.code in FATAL_VIOLATION_CODES for v in self.violations):
            raise ValueError("fatal 与 violations 不一致")
        if self.is_valid and (self.normalized_sql is None or self.limit_applied is None):
            raise ValueError("通过的结果必须给出 normalized_sql 与 limit_applied")
        if not self.is_valid and (self.normalized_sql is not None or not self.error_hint):
            raise ValueError("不通过的结果不得给出 normalized_sql，且必须给出 error_hint")
        return self


class ResultColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    data_type: ColumnDataType | None = Field(default=None, description="驱动可推断时填写")


class SqlExecutionResult(BaseModel):
    """ReadOnlySqlSandboxPort.execute 结果。SQL 层面的错误以 status + error_hint 返回而非抛异常。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: SqlExecutionStatus
    columns: list[ResultColumn] = Field(default_factory=list)
    rows: list[list[ScalarValue]] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0, description="返回的行数（截断后）")
    truncated: bool = Field(default=False, description="实际结果行数 > max_rows")
    elapsed_ms: float = Field(..., ge=0)
    error_code: str | None = Field(default=None, description="引擎原始错误类别（不含堆栈 / 连接串）")
    error_hint: str | None = Field(default=None, description="面向模型的列名 / 语法修正提示")

    @model_validator(mode="after")
    def _check_shape(self) -> SqlExecutionResult:
        if self.status is SqlExecutionStatus.SUCCESS:
            if self.error_hint is not None:
                raise ValueError("成功结果不得携带 error_hint")
            if self.row_count != len(self.rows):
                raise ValueError("row_count 必须等于 len(rows)")
            if any(len(r) != len(self.columns) for r in self.rows):
                raise ValueError("每行宽度必须等于列数")
        elif self.rows or not self.error_hint:
            raise ValueError("失败结果不得携带数据行，且必须给出 error_hint")
        return self


class SqlAttempt(BaseModel):
    """Text-to-SQL 自纠错循环的一次尝试（sql_attempts 轨迹元素）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_index: int = Field(..., ge=1, description="1 起连续递增，<= max_sql_attempts")
    candidate_sql: str = Field(..., description="模型原始输出（展示前经 mask_sql_for_display）")
    validation: SqlValidationResult
    execution: SqlExecutionResult | None = Field(default=None, description="守卫拒绝时为 None（未执行）")
    outcome: AttemptOutcome
    repair_feedback: str | None = Field(default=None, description="本次失败回灌下一轮 repair_sql 的 error_hint")
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    elapsed_ms: float = Field(..., ge=0)

    @model_validator(mode="after")
    def _check_outcome(self) -> SqlAttempt:
        executed = self.execution is not None
        if self.outcome in (AttemptOutcome.GUARD_REJECTED, AttemptOutcome.GUARD_FATAL):
            if executed or self.validation.is_valid:
                raise ValueError("守卫拒绝的尝试不得执行")
            if (self.outcome is AttemptOutcome.GUARD_FATAL) != self.validation.fatal:
                raise ValueError("GUARD_FATAL 与 validation.fatal 不一致")
        elif not executed or not self.validation.is_valid:
            raise ValueError("执行类 outcome 必须先通过守卫并带 execution")
        elif (self.outcome is AttemptOutcome.SUCCEEDED) != (self.execution.status is SqlExecutionStatus.SUCCESS):
            raise ValueError("SUCCEEDED 与 execution.status 不一致")
        return self


# ---------------------------------------------------------------------------
# 模型传输（SqlGeneratorModelPort 的输入 / 输出 schema）
# ---------------------------------------------------------------------------


class SqlModelResult(BaseModel, Generic[T]):
    """SqlGeneratorModelPort 统一返回包装。"""

    output: T
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_name: str = Field(default="", description="实际调用的模型 ID，进入轨迹审计")


class TimeRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _check_order(self) -> TimeRange:
        if self.start_date > self.end_date:
            raise ValueError("start_date 不得晚于 end_date")
        return self


class AnalysisIntent(BaseModel):
    """意图识别结构化输出。不含任何身份 / 范围取值字段（extra="forbid" 防注入）。

    mentioned_region_ids / mentioned_warehouse_ids 仅用于判断是否"明确要求越权范围"，
    只能收窄、不能放宽 WarehouseScopeFilter；SQL 中依旧只使用绑定参数。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_type: AnalysisIntentType
    confidence: float = Field(..., ge=0, le=1)
    metric_ids: list[str] = Field(default_factory=list, description="匹配的标准指标 ID（必须来自目录）")
    dimensions: list[str] = Field(default_factory=list, description="下钻维度列名")
    time_range: TimeRange | None = Field(default=None, description="None → 使用指标 default_lookback_days")
    mentioned_region_ids: list[str] = Field(default_factory=list)
    mentioned_warehouse_ids: list[str] = Field(default_factory=list)
    asks_national_view: bool = Field(default=False, description="明确要求全国 / 全集团口径")
    clarification_question: str | None = Field(default=None, max_length=200, description="指标歧义时的追问")


class SqlCandidate(BaseModel):
    """generate_sql / repair_sql 结构化输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sql: str = Field(..., min_length=1, max_length=8000)
    metric_ids: list[str] = Field(default_factory=list, description="本 SQL 实现的指标")
    rationale: str = Field(default="", max_length=500, description="口径说明，进入审计，不展示给用户")


class KeyFigure(BaseModel):
    """结论中引用的关键数值，必须锚定到结果表的具体单元格。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., min_length=1, max_length=64)
    value: float
    unit: MetricUnit
    row_index: int = Field(..., ge=0, description="ResultTable.rows 的 0-based 行号")
    column: str = Field(..., min_length=1)


class AnalysisNarrativeDraft(BaseModel):
    """summarize_analysis 结构化输出（数值一致性核验前）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conclusion: str = Field(..., min_length=1, max_length=1500)
    key_figures: list[KeyFigure] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list, max_length=3)


# ---------------------------------------------------------------------------
# 业务输出
# ---------------------------------------------------------------------------


class InventoryAlert(BaseModel):
    """库存预警（由 agent._build_alerts 按 AlertRuleConfig 确定性生成，模型不参与）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str = Field(..., pattern=r"^[0-9a-f]{16}$", description="sha256(alert_type|warehouse|material|batch|as_of)[:16]，幂等")
    alert_type: AlertType
    severity: AlertSeverity
    region_id: str
    warehouse_id: str
    material_id: str
    material_name: str
    batch_no: str | None = Field(default=None, description="NEAR_EXPIRY_OVERSTOCK 必填")
    metric_id: str
    current_value: float = Field(..., description="可用库存量 / 预计消化天数")
    threshold_value: float = Field(..., description="安全库存量 / 剩余效期天数")
    gap_qty: float = Field(..., ge=0, description="缺口量（补货）或积压量（调拨 / 报损）")
    quantity_unit: str = Field(..., max_length=16)
    days_to_expiry: int | None = Field(default=None)
    expiry_date: date | None = None
    recommended_action: AlertAction
    suggested_qty: float = Field(..., ge=0)
    counterpart_warehouse_id: str | None = Field(default=None, description="调拨对手仓，必须 ∈ 授权范围")
    as_of_date: date
    rationale: str = Field(..., max_length=300, description="确定性模板生成的中文说明")

    @model_validator(mode="after")
    def _check_type(self) -> InventoryAlert:
        if self.alert_type is AlertType.NEAR_EXPIRY_OVERSTOCK and (self.batch_no is None or self.expiry_date is None):
            raise ValueError("临期积压预警必须给出 batch_no 与 expiry_date")
        transfer = self.recommended_action in (AlertAction.TRANSFER_IN, AlertAction.TRANSFER_OUT)
        if transfer != (self.counterpart_warehouse_id is not None):
            raise ValueError("仅调拨类建议携带 counterpart_warehouse_id")
        if self.counterpart_warehouse_id == self.warehouse_id:
            raise ValueError("调拨对手仓不得为本仓")
        return self


class ResultTable(BaseModel):
    """响应中的结果摘要表（展示用截断，完整行数见 total_row_count）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: list[ResultColumn]
    rows: list[list[ScalarValue]] = Field(default_factory=list)
    total_row_count: int = Field(..., ge=0, description="沙箱返回行数")
    displayed_row_count: int = Field(..., ge=0)
    source_truncated: bool = Field(default=False, description="沙箱侧已按 max_rows 截断")


class AlertRuleConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    p1_days_of_supply: float = Field(default=1.0, gt=0, description="可用库存 / 日均出库 ≤ 此值 → P1")
    p2_days_of_supply: float = Field(default=3.0, gt=0)
    near_expiry_days: int = Field(default=7, ge=1, description="剩余效期 ≤ 此值的批次参与积压判定")
    p1_days_to_expiry: int = Field(default=3, ge=0)
    overstock_ratio: float = Field(default=1.0, gt=0, description="预计消化天数 > 剩余效期 × ratio → 积压")
    transfer_min_surplus_days: float = Field(default=7.0, gt=0, description="对手仓调出后仍需保有的供应天数")
    max_alerts: int = Field(default=50, ge=1, le=500)


class SupplyChainAgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_sql_attempts: int = Field(default=3, ge=1, le=5, description="N：生成 1 次 + 自纠错 N-1 次")
    model_timeout_s: float = Field(default=20.0, gt=0)
    sql_timeout_s: float = Field(default=5.0, gt=0, le=60)
    max_result_rows: int = Field(default=500, ge=1, le=10_000, description="沙箱返回上限，同时为 LIMIT 上限")
    display_rows: int = Field(default=20, ge=1, le=200, description="ResultTable 展示行数")
    intent_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    enable_alert_scan: bool = True
    alert_rules: AlertRuleConfig = Field(default_factory=AlertRuleConfig)


class SupplyChainQueryRequest(BaseModel):
    """分析请求（agent 内部入口）。user_context 必须来自网关头或 AgentInvocation。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    question: str = Field(..., min_length=1, max_length=1000)
    user_context: UserContext
    trace_id: str | None = Field(default=None, max_length=256)
    as_of_date: date | None = Field(default=None, description="分析基准日；None → 沙箱最新快照日（测试必须显式传入）")
    include_alerts: bool = True


class SupplyChainQueryPayload(BaseModel):
    """HTTP 请求体；user_context 仅在 allow_body_identity=True 的内网调试下生效。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(..., min_length=1, max_length=1000)
    user_context: UserContext | None = None
    trace_id: str | None = Field(default=None, max_length=256)
    as_of_date: date | None = None
    include_alerts: bool = True


class SupplyChainQueryResponse(BaseModel):
    """分析响应。追问 / 拒答 / 重试耗尽 / 数据源不可用均为 HTTP 200 的正常响应。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str | None = None
    stop_reason: StopReason
    refused: bool = False
    refusal_reason: RefusalReason | None = None
    conclusion: str = Field(..., min_length=1, description="自然语言分析结论 / 追问 / 拒答话术")
    narrative_source: NarrativeSource = NarrativeSource.NOT_APPLICABLE
    matched_metric_ids: list[str] = Field(default_factory=list)
    scope_summary: str = Field(..., description="确定性渲染的生效范围说明，如 华东大区 · 授权仓 2 个")
    as_of_date: date | None = None
    final_sql: str | None = Field(default=None, description="最终执行的脱敏 SQL（绑定参数保持占位符）")
    result_table: ResultTable | None = None
    alerts: list[InventoryAlert] = Field(default_factory=list)
    sql_attempts: list[SqlAttempt] = Field(default_factory=list)
    referenced_tables: list[str] = Field(default_factory=list)
    latency_ms: float = Field(..., ge=0)
    stage_latency_ms: dict[LoopStage, float] = Field(default_factory=dict)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    token_cost: int = Field(default=0, ge=0, description="== token_usage.total_tokens（仅被测系统自身消耗）")

    @model_validator(mode="after")
    def _check_consistency(self) -> SupplyChainQueryResponse:
        if self.refused != (self.stop_reason is StopReason.REFUSED):
            raise ValueError("refused 必须等价于 stop_reason == REFUSED")
        if self.refused != (self.refusal_reason is not None):
            raise ValueError("refusal_reason 仅在拒答时给出")
        answered = self.stop_reason is StopReason.ANSWERED
        if answered and (self.final_sql is None or self.result_table is None or not self.sql_attempts):
            raise ValueError("ANSWERED 必须给出 final_sql、result_table 与 sql_attempts")
        if not answered and (self.final_sql is not None or self.result_table is not None):
            raise ValueError("非 ANSWERED 不得给出 final_sql / result_table")
        if answered == (self.narrative_source is NarrativeSource.NOT_APPLICABLE):
            raise ValueError("narrative_source 与 stop_reason 不一致")
        if [a.attempt_index for a in self.sql_attempts] != list(range(1, len(self.sql_attempts) + 1)):
            raise ValueError("sql_attempts.attempt_index 必须从 1 连续递增")
        if self.token_cost != self.token_usage.total_tokens:
            raise ValueError("token_cost 必须等于 token_usage.total_tokens")
        return self


class AnalysisOutcome(BaseModel):
    """_run_pipeline 的内部产物：响应 + 评测映射所需的中间结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    response: SupplyChainQueryResponse
    scope: WarehouseScopeFilter | None = None
    intent: AnalysisIntent | None = None
    final_execution: SqlExecutionResult | None = Field(default=None, description="ANSWERED 时为成功执行结果（未截断展示）")
