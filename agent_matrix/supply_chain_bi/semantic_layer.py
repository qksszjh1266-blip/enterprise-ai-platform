"""餐饮供应链语义层目录。

两部分：
1. SUPPLY_CHAIN_CATALOG：语义字典**配置**（完整可用，接口契约）。物理表结构与 tests/agent_matrix/supply_chain_bi/schema.sql 中的模拟库一致。
2. SemanticLayer：目录访问 / 指标匹配 / 语义上下文裁剪 / 范围谓词渲染 / 预警模板渲染 / 启动自检（待实现）。

范围谓词约定（模型与内置模板共用，SqlStaticGuard 以此校验）：

    ScopeLevel   每个受控表引用 <alias> 必须在其所在 SELECT 的 WHERE 顶层 AND 合取中出现
    WAREHOUSE    <alias>.region_id = :scope_region_id AND <alias>.warehouse_id IN :scope_warehouse_ids
    REGION       <alias>.region_id = :scope_region_id
    NATIONAL     （无需范围谓词）

    - 受控表 = TableSchemaSpec.requires_scope_filter=True；dim_material 为全局维表，不受控。
    - 内置模板用 string.Template 占位 `${scope_<alias>}`，由 render_alert_sql 按范围层级替换为上表谓词；
      无谓词层级（NATIONAL）替换为 `1 = 1`。
    - 基准日一律使用 :as_of_date，禁止 CURRENT_DATE / date('now')（结果可复现、评测可回放）。

目录方言：SQLite（模拟库与单测）。PostgreSQL 生产目录需另建同结构实例（julianday → date 差值），不在当前 SQLite 适配器范围。
修改本文件任何表 / 列 / 指标 / 模板必须递增 catalog_version。
"""

from __future__ import annotations

import unicodedata
from string import Template
from datetime import datetime, timezone
from agent_matrix.supply_chain_bi.models import WarehouseScopeFilter
from agent_matrix.supply_chain_bi.errors import SemanticCatalogError

from datetime import date

from agent_matrix.supply_chain_bi.models import (
    AlertQueryTemplate,
    AlertType,
    ColumnDataType as T,
    ColumnSpec as C,
    MetricDefinition,
    MetricUnit,
    ScopeLevel,
    SemanticCatalogSpec,
    SemanticContext,
    SqlDialect,
    TableSchemaSpec,
)

# ---------------------------------------------------------------------------
# 物理表（列白名单 + 敏感列黑名单）
# ---------------------------------------------------------------------------

DIM_WAREHOUSE = TableSchemaSpec(
    table_name="dim_warehouse",
    display_name="仓库维表",
    description="中心仓 / 区域仓 / 前置仓主数据",
    grain="仓库",
    columns=[
        C(name="warehouse_id", data_type=T.STRING, description="仓库编码", is_dimension=True),
        C(name="warehouse_name", data_type=T.STRING, description="仓库名称", is_dimension=True),
        C(name="region_id", data_type=T.STRING, description="所属大区编码", is_dimension=True),
        C(name="region_name", data_type=T.STRING, description="所属大区名称", is_dimension=True),
        C(name="warehouse_type", data_type=T.STRING, description="central / regional / front", is_dimension=True),
        C(name="is_active", data_type=T.BOOLEAN, description="是否在营"),
    ],
)

DIM_STORE = TableSchemaSpec(
    table_name="dim_store",
    display_name="门店维表",
    description="门店主数据及其配送仓归属",
    grain="门店",
    columns=[
        C(name="store_id", data_type=T.STRING, description="门店编码", is_dimension=True),
        C(name="store_name", data_type=T.STRING, description="门店名称", is_dimension=True),
        C(name="region_id", data_type=T.STRING, description="所属大区编码", is_dimension=True),
        C(name="warehouse_id", data_type=T.STRING, description="配送仓编码", is_dimension=True),
        C(name="city", data_type=T.STRING, description="城市", is_dimension=True),
        C(name="store_tier", data_type=T.STRING, description="门店等级 A/B/C", is_dimension=True),
    ],
    sensitive_columns=["franchisee_phone"],
)

DIM_MATERIAL = TableSchemaSpec(
    table_name="dim_material",
    display_name="原料维表",
    description="原料 / 包材主数据（全局共享，无行级范围）",
    grain="原料",
    columns=[
        C(name="material_id", data_type=T.STRING, description="原料编码", is_dimension=True),
        C(name="material_name", data_type=T.STRING, description="原料名称，如 柠檬 / 珍珠 / 冰淇淋粉", is_dimension=True),
        C(name="category", data_type=T.STRING, description="fresh / frozen / dry / packaging", is_dimension=True),
        C(name="base_unit", data_type=T.STRING, description="计量单位，如 kg / 箱 / 袋", is_dimension=True),
        C(name="shelf_life_days", data_type=T.INTEGER, description="标准保质期天数"),
        C(name="storage_condition", data_type=T.STRING, description="ambient / chilled / frozen", is_dimension=True),
    ],
    region_column=None,
    warehouse_column=None,
    requires_scope_filter=False,
)

FACT_STORE_SALES_DAILY = TableSchemaSpec(
    table_name="fact_store_sales_daily",
    display_name="门店日销量",
    description="门店×饮品 SKU×日 的销量与零售营收",
    grain="门店×SKU×日",
    columns=[
        C(name="biz_date", data_type=T.DATE, description="营业日期", is_dimension=True),
        C(name="store_id", data_type=T.STRING, description="门店编码", is_dimension=True),
        C(name="region_id", data_type=T.STRING, description="大区编码", is_dimension=True),
        C(name="warehouse_id", data_type=T.STRING, description="配送仓编码", is_dimension=True),
        C(name="sku_id", data_type=T.STRING, description="饮品 SKU 编码", is_dimension=True),
        C(name="sku_name", data_type=T.STRING, description="饮品名称", is_dimension=True),
        C(name="cups_sold", data_type=T.INTEGER, description="售出杯数", unit="杯"),
        C(name="net_revenue", data_type=T.DECIMAL, description="零售净营收（元）", unit="元"),
    ],
    sensitive_columns=["sku_standard_cost"],
    time_column="biz_date",
)

FACT_WAREHOUSE_INVENTORY_DAILY = TableSchemaSpec(
    table_name="fact_warehouse_inventory_daily",
    display_name="仓库日库存快照",
    description="仓库×原料×日 的日终库存、在途、占用、安全库存与出库",
    grain="仓库×原料×日",
    columns=[
        C(name="snapshot_date", data_type=T.DATE, description="快照日期（日终）", is_dimension=True),
        C(name="region_id", data_type=T.STRING, description="大区编码", is_dimension=True),
        C(name="warehouse_id", data_type=T.STRING, description="仓库编码", is_dimension=True),
        C(name="material_id", data_type=T.STRING, description="原料编码", is_dimension=True),
        C(name="on_hand_qty", data_type=T.DECIMAL, description="在库量"),
        C(name="in_transit_qty", data_type=T.DECIMAL, description="采购 / 调拨在途量"),
        C(name="reserved_qty", data_type=T.DECIMAL, description="已被门店订单占用量"),
        C(name="safety_stock_qty", data_type=T.DECIMAL, description="安全库存量（计划系统下发）"),
        C(name="outbound_qty", data_type=T.DECIMAL, description="当日出库量"),
        C(name="avg_daily_outbound_7d", data_type=T.DECIMAL, description="近 7 日日均出库量（含当日）"),
    ],
    sensitive_columns=["inventory_cost_amount"],
    time_column="snapshot_date",
)

FACT_INVENTORY_BATCH = TableSchemaSpec(
    table_name="fact_inventory_batch",
    display_name="库存批次效期",
    description="仓库在库批次的生产 / 到期日期与剩余量（按快照日）",
    grain="仓库×批次×快照日",
    columns=[
        C(name="snapshot_date", data_type=T.DATE, description="快照日期", is_dimension=True),
        C(name="batch_no", data_type=T.STRING, description="批次号", is_dimension=True),
        C(name="region_id", data_type=T.STRING, description="大区编码", is_dimension=True),
        C(name="warehouse_id", data_type=T.STRING, description="仓库编码", is_dimension=True),
        C(name="material_id", data_type=T.STRING, description="原料编码", is_dimension=True),
        C(name="supplier_id", data_type=T.STRING, description="供应商编码（不含价格）", is_dimension=True),
        C(name="production_date", data_type=T.DATE, description="生产日期"),
        C(name="expiry_date", data_type=T.DATE, description="到期日期"),
        C(name="inbound_date", data_type=T.DATE, description="入库日期"),
        C(name="inbound_qty", data_type=T.DECIMAL, description="入库量"),
        C(name="remaining_qty", data_type=T.DECIMAL, description="剩余量"),
    ],
    sensitive_columns=["supplier_cost_price"],
    time_column="snapshot_date",
)

FACT_MATERIAL_LOSS = TableSchemaSpec(
    table_name="fact_material_loss",
    display_name="原料损耗明细",
    description="仓库原料报损记录（过期 / 破损 / 温控异常 / 其他）",
    grain="报损单行",
    columns=[
        C(name="loss_date", data_type=T.DATE, description="报损日期", is_dimension=True),
        C(name="region_id", data_type=T.STRING, description="大区编码", is_dimension=True),
        C(name="warehouse_id", data_type=T.STRING, description="仓库编码", is_dimension=True),
        C(name="material_id", data_type=T.STRING, description="原料编码", is_dimension=True),
        C(name="batch_no", data_type=T.STRING, description="批次号", is_dimension=True),
        C(name="loss_qty", data_type=T.DECIMAL, description="报损量"),
        C(name="loss_reason", data_type=T.STRING, description="expired / damaged / temperature / other", is_dimension=True),
    ],
    sensitive_columns=["loss_cost_amount"],
    time_column="loss_date",
)

# ---------------------------------------------------------------------------
# 标准业务指标
# ---------------------------------------------------------------------------

MATERIAL_LOSS_RATE = MetricDefinition(
    metric_id="material_loss_rate",
    display_name="原料损耗率",
    aliases=["损耗率", "报损率", "损耗占比", "原料浪费率"],
    calculation_caliber=(
        "统计期内 报损量 ÷ (出库量 + 报损量)。报损量取 fact_material_loss.loss_qty（全部原因），"
        "出库量取 fact_warehouse_inventory_daily.outbound_qty；两表须先分别按 仓库×原料 聚合再关联，"
        "禁止明细直接 JOIN 导致重复计数；分母为 0 记为 NULL。统计期缺省为基准日前 30 天（含基准日）。"
    ),
    formula_sql="SUM(l.loss_qty) / NULLIF(SUM(i.outbound_qty) + SUM(l.loss_qty), 0)",
    unit=MetricUnit.RATIO,
    decimal_places=4,
    source_tables=["fact_material_loss", "fact_warehouse_inventory_daily"],
    required_columns={
        "fact_material_loss": ["loss_date", "warehouse_id", "material_id", "loss_qty"],
        "fact_warehouse_inventory_daily": ["snapshot_date", "warehouse_id", "material_id", "outbound_qty"],
    },
    dimensions=["warehouse_id", "material_id", "loss_reason"],
    default_lookback_days=30,
    higher_is_better=False,
)

SAFETY_STOCK_GAP = MetricDefinition(
    metric_id="safety_stock_gap",
    display_name="安全库存缺口",
    aliases=["安全库存差额", "低于安全库存", "缺货风险", "补货量"],
    calculation_caliber=(
        "基准日快照：可用库存 = 在库量 + 在途量 − 占用量；缺口 = MAX(安全库存量 − 可用库存, 0)。"
        "缺口 > 0 即判定跌破安全库存。仅取 snapshot_date = :as_of_date 的快照。"
    ),
    formula_sql="MAX(i.safety_stock_qty - (i.on_hand_qty + i.in_transit_qty - i.reserved_qty), 0)",
    unit=MetricUnit.QUANTITY,
    decimal_places=2,
    source_tables=["fact_warehouse_inventory_daily"],
    required_columns={
        "fact_warehouse_inventory_daily": [
            "snapshot_date", "warehouse_id", "material_id",
            "on_hand_qty", "in_transit_qty", "reserved_qty", "safety_stock_qty",
        ],
    },
    dimensions=["warehouse_id", "material_id"],
    default_lookback_days=1,
    higher_is_better=False,
)

SHELF_LIFE_TURNOVER_DAYS = MetricDefinition(
    metric_id="shelf_life_turnover_days",
    display_name="效期周转天数",
    aliases=["效期周转", "批次消化天数", "临期周转", "多久能用完"],
    calculation_caliber=(
        "批次维度：预计消化天数 = 批次剩余量 ÷ 所在仓该原料近 7 日日均出库量；"
        "剩余效期天数 = expiry_date − :as_of_date。预计消化天数 > 剩余效期天数 即存在临期积压风险。"
        "日均出库为 0 时消化天数记为 NULL（视为无法消化）。仅取 snapshot_date = :as_of_date 的批次与库存快照。"
    ),
    formula_sql=(
        "b.remaining_qty / NULLIF(i.avg_daily_outbound_7d, 0) AS turnover_days, "
        "julianday(b.expiry_date) - julianday(:as_of_date) AS days_to_expiry"
    ),
    unit=MetricUnit.DAYS,
    decimal_places=1,
    source_tables=["fact_inventory_batch", "fact_warehouse_inventory_daily"],
    required_columns={
        "fact_inventory_batch": ["snapshot_date", "batch_no", "warehouse_id", "material_id", "expiry_date", "remaining_qty"],
        "fact_warehouse_inventory_daily": ["snapshot_date", "warehouse_id", "material_id", "avg_daily_outbound_7d"],
    },
    dimensions=["warehouse_id", "material_id", "batch_no"],
    default_lookback_days=1,
    higher_is_better=False,
)

DAYS_OF_SUPPLY = MetricDefinition(
    metric_id="days_of_supply",
    display_name="库存可供天数",
    aliases=["可售天数", "库存天数", "还能撑几天", "库存周转天数"],
    calculation_caliber="基准日快照：可用库存（在库 + 在途 − 占用）÷ 近 7 日日均出库量；日均出库为 0 记为 NULL。",
    formula_sql="(i.on_hand_qty + i.in_transit_qty - i.reserved_qty) / NULLIF(i.avg_daily_outbound_7d, 0)",
    unit=MetricUnit.DAYS,
    decimal_places=1,
    source_tables=["fact_warehouse_inventory_daily"],
    required_columns={
        "fact_warehouse_inventory_daily": [
            "snapshot_date", "warehouse_id", "material_id",
            "on_hand_qty", "in_transit_qty", "reserved_qty", "avg_daily_outbound_7d",
        ],
    },
    dimensions=["warehouse_id", "material_id"],
    default_lookback_days=1,
    higher_is_better=None,
)

NEAR_EXPIRY_QTY = MetricDefinition(
    metric_id="near_expiry_qty",
    display_name="临期库存量",
    aliases=["临期量", "快过期", "即将过期库存"],
    calculation_caliber="基准日快照：剩余效期天数（expiry_date − :as_of_date）在 [0, 7] 内的批次 remaining_qty 合计。",
    formula_sql="SUM(CASE WHEN julianday(b.expiry_date) - julianday(:as_of_date) BETWEEN 0 AND 7 THEN b.remaining_qty ELSE 0 END)",
    unit=MetricUnit.QUANTITY,
    decimal_places=2,
    source_tables=["fact_inventory_batch"],
    required_columns={"fact_inventory_batch": ["snapshot_date", "warehouse_id", "material_id", "expiry_date", "remaining_qty"]},
    dimensions=["warehouse_id", "material_id"],
    default_lookback_days=1,
    higher_is_better=False,
)

STORE_CUPS_SOLD = MetricDefinition(
    metric_id="store_cups_sold",
    display_name="门店杯量",
    aliases=["销量", "出杯量", "卖了多少杯"],
    calculation_caliber="统计期内 fact_store_sales_daily.cups_sold 合计；统计期缺省为基准日前 7 天（含基准日）。",
    formula_sql="SUM(s.cups_sold)",
    unit=MetricUnit.CUPS,
    decimal_places=0,
    source_tables=["fact_store_sales_daily"],
    required_columns={"fact_store_sales_daily": ["biz_date", "store_id", "sku_id", "cups_sold"]},
    dimensions=["store_id", "sku_name", "warehouse_id"],
    default_lookback_days=7,
    higher_is_better=True,
)

# ---------------------------------------------------------------------------
# 内置预警扫描模板（确定性，不经模型；同样必须通过守卫）
# ---------------------------------------------------------------------------

ALERT_INVENTORY_POSITION = AlertQueryTemplate(
    template_id="alert_inventory_position",
    alert_type=AlertType.SAFETY_STOCK_BREACH,
    metric_id="safety_stock_gap",
    sql=(
        "SELECT i.region_id, i.warehouse_id, i.material_id, m.material_name, m.base_unit, "
        "i.on_hand_qty + i.in_transit_qty - i.reserved_qty AS available_qty, "
        "i.safety_stock_qty, i.avg_daily_outbound_7d "
        "FROM fact_warehouse_inventory_daily i "
        "JOIN dim_material m ON m.material_id = i.material_id "
        "WHERE i.snapshot_date = :as_of_date AND ${scope_i} "
        "ORDER BY i.material_id, i.warehouse_id"
    ),
    expected_columns=[
        "region_id", "warehouse_id", "material_id", "material_name", "base_unit",
        "available_qty", "safety_stock_qty", "avg_daily_outbound_7d",
    ],
)
"""返回范围内**全部**仓×原料库存位置（不只缺口行），规则引擎据此同时判定跌破与寻找授权范围内的富余调出仓。"""

ALERT_NEAR_EXPIRY_BATCHES = AlertQueryTemplate(
    template_id="alert_near_expiry_batches",
    alert_type=AlertType.NEAR_EXPIRY_OVERSTOCK,
    metric_id="shelf_life_turnover_days",
    sql=(
        "SELECT b.region_id, b.warehouse_id, b.material_id, m.material_name, m.base_unit, b.batch_no, "
        "b.expiry_date, julianday(b.expiry_date) - julianday(:as_of_date) AS days_to_expiry, "
        "b.remaining_qty, i.avg_daily_outbound_7d "
        "FROM fact_inventory_batch b "
        "JOIN dim_material m ON m.material_id = b.material_id "
        "LEFT JOIN fact_warehouse_inventory_daily i "
        "ON i.warehouse_id = b.warehouse_id AND i.material_id = b.material_id AND i.snapshot_date = b.snapshot_date "
        "AND ${scope_i} "
        "WHERE b.snapshot_date = :as_of_date AND ${scope_b} AND b.remaining_qty > 0 "
        "AND julianday(b.expiry_date) - julianday(:as_of_date) <= 30 "
        "ORDER BY days_to_expiry, b.batch_no"
    ),
    expected_columns=[
        "region_id", "warehouse_id", "material_id", "material_name", "base_unit", "batch_no",
        "expiry_date", "days_to_expiry", "remaining_qty", "avg_daily_outbound_7d",
    ],
)
"""LEFT JOIN 的受控表 i 范围谓词放在 ON 子句：守卫对 JOIN 右表接受 ON 子句顶层 AND 合取中的范围谓词。"""

# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------

SUPPLY_CHAIN_CATALOG = SemanticCatalogSpec(
    catalog_version=1,
    dialect=SqlDialect.SQLITE,
    tables=[
        DIM_WAREHOUSE, DIM_STORE, DIM_MATERIAL,
        FACT_STORE_SALES_DAILY, FACT_WAREHOUSE_INVENTORY_DAILY, FACT_INVENTORY_BATCH, FACT_MATERIAL_LOSS,
    ],
    metrics=[
        MATERIAL_LOSS_RATE, SAFETY_STOCK_GAP, SHELF_LIFE_TURNOVER_DAYS,
        DAYS_OF_SUPPLY, NEAR_EXPIRY_QTY, STORE_CUPS_SOLD,
    ],
    alert_templates=[ALERT_INVENTORY_POSITION, ALERT_NEAR_EXPIRY_BATCHES],
    global_sensitive_columns=[
        "supplier_cost_price", "contract_price", "rebate_rate", "purchase_amount",
        "inventory_cost_amount", "loss_cost_amount", "sku_standard_cost", "franchisee_phone",
    ],
    forbidden_tables=["dim_supplier_contract", "fin_purchase_settlement", "fin_store_profit_daily"],
    system_catalog_patterns=["sqlite_", "information_schema", "pg_", "mysql", "performance_schema", "sys"],
    forbidden_functions=[
        "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "randomblob", "random",
        "sqlite_version", "sqlite_source_id", "sqlite_compileoption_get", "sqlite_offset",
        "current_date", "current_time", "current_timestamp", "now", "localtimestamp",
        "pg_sleep", "pg_read_file", "pg_ls_dir", "lo_import", "lo_export", "dblink", "set_config", "current_setting",
    ],
)
"""SQLite 模拟库语义目录（冻结配置）。date()/julianday() 允许，但参数为 'now' 字面量时守卫按 FORBIDDEN_FUNCTION 拦截。"""

SCOPE_TEMPLATE_PREFIX = "scope_"
"""预警模板占位符前缀：`${scope_<alias>}`。"""


class SemanticLayer:
    """语义目录访问与上下文裁剪（纯内存、无 I/O）。"""

    def __init__(self, catalog: SemanticCatalogSpec = SUPPLY_CHAIN_CATALOG) -> None:
        """保存目录并建立 table_name / metric_id / alias 索引（仅构造索引，不做自检；自检见 self_check）。"""
        self._catalog = catalog
        self._tables = {t.table_name: t for t in catalog.tables}
        self._metrics = {m.metric_id: m for m in catalog.metrics}

    @property
    def catalog(self) -> SemanticCatalogSpec:
        return self._catalog

    # ---------------- 查询 ----------------

    def get_table(self, table_name: str) -> TableSchemaSpec | None:
        """按小写表名精确查找白名单表；forbidden_tables / 未登记表返回 None。"""
        return self._tables.get(table_name.lower()) if table_name.lower() not in self.catalog.forbidden_tables else None

    def get_metric(self, metric_id: str) -> MetricDefinition | None:
        return self._metrics.get(metric_id)

    def is_sensitive_column(self, table_name: str | None, column: str) -> bool:
        """column ∈ global_sensitive_columns ∪ (table.sensitive_columns)；table_name 为 None 时只看全局黑名单。比较前统一小写。"""
        table = self.get_table(table_name) if table_name else None
        return column.lower() in {c.lower() for c in self.catalog.global_sensitive_columns + (table.sensitive_columns if table else [])}

    def is_system_catalog(self, table_name: str) -> bool:
        """小写后等于或以任一 system_catalog_patterns 开头（含 schema 前缀，如 main.sqlite_master / pg_catalog.pg_user）。"""
        return any(part.startswith(p) for part in table_name.lower().split('.') for p in self.catalog.system_catalog_patterns)

    # ---------------- 语义匹配 ----------------

    def match_metrics(self, question: str) -> list[MetricDefinition]:
        """确定性同义词匹配（NFKC + casefold 后对 display_name ∪ aliases 子串匹配），按目录顺序去重返回。

        用途：① 作为 classify_intent 结果的校验集（模型给出目录外 ID 时丢弃）；② 模型置信度不足时的追问候选。
        """
        normalize = lambda s: unicodedata.normalize("NFKC", s).casefold()
        text = normalize(question)
        return [m for m in self.catalog.metrics if any(normalize(a) in text for a in [m.display_name, *m.aliases])]

    def filter_metric_ids(self, metric_ids: list[str]) -> list[str]:
        """只保留目录中存在的 metric_id（保序去重）。"""
        return list(dict.fromkeys(m for m in metric_ids if m in self._metrics))

    # ---------------- 上下文 ----------------

    def build_context(
        self,
        *,
        metric_ids: list[str],
        scope_level: ScopeLevel,
        as_of_date: date,
        max_rows: int,
    ) -> SemanticContext:
        """裁剪送入模型的语义上下文。

        - tables = 指标 source_tables ∪ {dim_material, dim_warehouse}（metric_ids 为空时给全部白名单表）；
        - 每张表 sensitive_columns 置空（敏感列名永不进入 prompt）；
        - scope_instructions = render_scope_instructions(scope_level)。
        """
        metrics = [self._metrics[m] for m in self.filter_metric_ids(metric_ids)]
        names = {t for m in metrics for t in m.source_tables} | {"dim_material", "dim_warehouse"}
        tables = [t.model_copy(update={"sensitive_columns": []}, deep=True) for t in self.catalog.tables if not metric_ids or t.table_name in names]
        return SemanticContext(catalog_version=self.catalog.catalog_version, dialect=self.catalog.dialect, metrics=metrics if metric_ids else list(self.catalog.metrics), tables=tables, scope_level=scope_level, scope_instructions=self.render_scope_instructions(scope_level), as_of_date=as_of_date, max_rows=max_rows)

    def render_scope_instructions(self, scope_level: ScopeLevel) -> str:
        """确定性渲染范围谓词书写规则（中文，含模块文档表格中对应层级的示例谓词），不含任何范围取值。"""
        return "每个受控表均须在本层 WHERE 顶层 AND 合取中添加范围谓词，外连接右表可置于 ON；范围值仅由服务绑定。示例：" + self.render_scope_predicate("i", scope_level)

    def render_scope_predicate(self, alias: str, scope_level: ScopeLevel) -> str:
        """按模块文档表格返回 `<alias>.region_id = :scope_region_id ...`；NATIONAL 返回 `1 = 1`。"""
        if scope_level is ScopeLevel.NATIONAL:
            return "1 = 1"
        result = f"{alias}.region_id = :scope_region_id"
        if scope_level is ScopeLevel.WAREHOUSE:
            result += f" AND {alias}.warehouse_id IN :scope_warehouse_ids"
        return result

    def render_alert_sql(self, template: AlertQueryTemplate, scope_level: ScopeLevel) -> str:
        """用 string.Template.substitute 将 `${scope_<alias>}` 替换为 render_scope_predicate；缺失占位符抛 KeyError。"""
        template_sql = Template(template.sql)
        return template_sql.substitute({name: self.render_scope_predicate(name[len(SCOPE_TEMPLATE_PREFIX):], scope_level) for name in template_sql.get_identifiers() if name.startswith(SCOPE_TEMPLATE_PREFIX)})

    def public_catalog(self) -> SemanticCatalogSpec:
        """对外（GET /catalog）输出的脱敏目录副本：清空各表 sensitive_columns、global_sensitive_columns 置为 ["***"]、
        清空 forbidden_tables / system_catalog_patterns / forbidden_functions 的明细（保留 min_length 约束所需的占位）。"""
        return self.catalog.model_copy(deep=True, update={"tables": [t.model_copy(deep=True, update={"sensitive_columns": []}) for t in self.catalog.tables], "global_sensitive_columns": ["***"], "forbidden_tables": [], "system_catalog_patterns": ["***"], "forbidden_functions": ["***"]})

    # ---------------- 自检 ----------------

    def self_check(self, guard: object) -> None:
        """启动自检：每个 alert_template × 每个 ScopeLevel（用合成的 WarehouseScopeFilter 样例）渲染后必须通过 guard.validate，
        且 referenced_columns 覆盖 expected_columns 所依赖的物理列；失败抛 SemanticCatalogError。

        guard 类型为 guardrails.SqlStaticGuard（此处用 object 避免循环导入）。
        """
        from sqlglot import parse_one, exp
        try:
            for template in self.catalog.alert_templates:
                for level in ScopeLevel:
                    scope = WarehouseScopeFilter(user_id="self_check", scope_level=level, region_id=None if level is ScopeLevel.NATIONAL else "TEST", authorized_warehouse_ids=["TEST"] if level is ScopeLevel.WAREHOUSE else [], resolved_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
                    result = guard.validate(self.render_alert_sql(template, level), scope=scope)
                    if not result.is_valid:
                        raise ValueError("模板未通过守卫")
                    tree = parse_one(result.normalized_sql, read=self.catalog.dialect.value)
                    if [e.alias_or_name for e in tree.expressions] != template.expected_columns:
                        raise ValueError("模板结果列与规则引擎不一致")
                    referenced = {c for cols in result.referenced_columns.values() for c in cols}
                    if any(c.name.lower() not in referenced for e in tree.expressions for c in e.find_all(exp.Column)):
                        raise ValueError("模板物理列依赖缺失")
        except Exception as exc:
            raise SemanticCatalogError("内置预警模板校验失败") from exc
