CREATE TABLE dim_warehouse (
  warehouse_id TEXT PRIMARY KEY, warehouse_name TEXT NOT NULL, region_id TEXT NOT NULL, region_name TEXT NOT NULL,
  warehouse_type TEXT NOT NULL CHECK (warehouse_type IN ('central','regional','front')), is_active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE dim_store (
  store_id TEXT PRIMARY KEY, store_name TEXT NOT NULL, region_id TEXT NOT NULL, warehouse_id TEXT NOT NULL REFERENCES dim_warehouse,
  city TEXT NOT NULL, store_tier TEXT NOT NULL, franchisee_phone TEXT);

CREATE TABLE dim_material (
  material_id TEXT PRIMARY KEY, material_name TEXT NOT NULL, category TEXT NOT NULL, base_unit TEXT NOT NULL,
  shelf_life_days INTEGER NOT NULL, storage_condition TEXT NOT NULL);

-- 门店销量
CREATE TABLE fact_store_sales_daily (
  biz_date TEXT NOT NULL, store_id TEXT NOT NULL, region_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
  sku_id TEXT NOT NULL, sku_name TEXT NOT NULL, cups_sold INTEGER NOT NULL, net_revenue REAL NOT NULL,
  sku_standard_cost REAL, PRIMARY KEY (biz_date, store_id, sku_id));

-- 仓库库存（日终快照）
CREATE TABLE fact_warehouse_inventory_daily (
  snapshot_date TEXT NOT NULL, region_id TEXT NOT NULL, warehouse_id TEXT NOT NULL, material_id TEXT NOT NULL,
  on_hand_qty REAL NOT NULL, in_transit_qty REAL NOT NULL DEFAULT 0, reserved_qty REAL NOT NULL DEFAULT 0,
  safety_stock_qty REAL NOT NULL, outbound_qty REAL NOT NULL DEFAULT 0, avg_daily_outbound_7d REAL NOT NULL DEFAULT 0,
  inventory_cost_amount REAL, PRIMARY KEY (snapshot_date, warehouse_id, material_id));

-- 批次效期
CREATE TABLE fact_inventory_batch (
  snapshot_date TEXT NOT NULL, batch_no TEXT NOT NULL, region_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
  material_id TEXT NOT NULL, supplier_id TEXT NOT NULL, production_date TEXT NOT NULL, expiry_date TEXT NOT NULL,
  inbound_date TEXT NOT NULL, inbound_qty REAL NOT NULL, remaining_qty REAL NOT NULL,
  supplier_cost_price REAL, PRIMARY KEY (snapshot_date, warehouse_id, batch_no));

-- 原料损耗
CREATE TABLE fact_material_loss (
  loss_id INTEGER PRIMARY KEY, loss_date TEXT NOT NULL, region_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
  material_id TEXT NOT NULL, batch_no TEXT, loss_qty REAL NOT NULL,
  loss_reason TEXT NOT NULL CHECK (loss_reason IN ('expired','damaged','temperature','other')), loss_cost_amount REAL);

-- 禁止表（物理存在，任何引用 = FATAL）
CREATE TABLE dim_supplier_contract (supplier_id TEXT, material_id TEXT, supplier_cost_price REAL, rebate_rate REAL, contract_no TEXT);
CREATE TABLE fin_purchase_settlement (settle_id TEXT, supplier_id TEXT, purchase_amount REAL, settle_month TEXT);
