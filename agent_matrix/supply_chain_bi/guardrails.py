"""SQL 静态安全守卫。

SqlStaticGuard.validate 是候选 SQL 进入只读沙箱前的**唯一闸门**；沙箱连接层只读保护是第二道防线，二者不可互相替代。

实现选型（冻结）：基于 AST（sqlglot，按 SemanticCatalogSpec.dialect 解析），**禁止**以正则 / 字符串包含作为放行依据
（正则仅可用于"额外拦截"，不可用于"放行"）。SQL AST 解析使用运行时依赖 sqlglot。
为保证 `create_app().openapi()` 在依赖缺失时仍可导出，sqlglot 只允许在方法内部延迟导入。

校验顺序（短路规则：解析失败直接返回；其余规则全部执行并汇总全部违规，便于模型一次改全）：

    #   规则                                        违规码                        fatal
    1   解析失败 / 空语句                            PARSE_ERROR / EMPTY_STATEMENT  否
    2   多语句（含末尾分号后的第二条）                 MULTI_STATEMENT                否
    3   顶层不是 SELECT / WITH…SELECT / UNION 查询     NON_SELECT_STATEMENT           否
    4   任意位置出现 INSERT/UPDATE/DELETE/MERGE/DDL/   WRITE_OPERATION                是
        REPLACE/ATTACH/DETACH/PRAGMA/VACUUM/SELECT INTO/可写 CTE/COPY/CALL/SET
    5   引用系统元数据表（含 schema 前缀与引号变体）    SYSTEM_CATALOG_ACCESS          是
    6   引用 forbidden_tables                         FORBIDDEN_TABLE                是
    7   引用未登记的物理表（CTE 名不算物理表）           TABLE_NOT_WHITELISTED          否
    8   任意位置引用敏感列（含别名解析、未限定列名）     SENSITIVE_COLUMN               是
    9   SELECT * / t.*（任意层级，含 COUNT(*) 之外）    SELECT_STAR_FORBIDDEN          否
    10  引用白名单表中未登记的列                        COLUMN_NOT_WHITELISTED         否
    11  调用 forbidden_functions，或 date/time 函数     FORBIDDEN_FUNCTION             是
        以 'now' 字面量为参数
    12  出现 RESERVED_BIND_PARAMS 以外的命名 / 位置参数   UNKNOWN_BIND_PARAM             否
    13  范围谓词：见下                                  SCOPE_*                        否
    14  LIMIT：缺失 → 追加 max_rows；> max_rows → 收紧为 max_rows；非整数字面量 → PARSE_ERROR

范围谓词规则（ScopeLevel 与谓词形态见 semantic_layer 模块文档）：
- 对每一个受控表引用（含 CTE / 子查询 / UNION 分支内部）定位其所在 SELECT：
  · FROM / INNER JOIN 表：谓词必须位于该 SELECT 的 WHERE 顶层 AND 合取中；
  · LEFT / RIGHT / FULL JOIN 的受控表：谓词必须位于该 JOIN 的 ON 顶层 AND 合取中（或 WHERE 顶层）。
- 谓词出现在 OR / NOT / CASE / 子查询比较等非合取位置 → SCOPE_PREDICATE_WEAKENED。
- 允许用字面量**额外收窄**（如用户问"WH_E01 仓"）：`... AND i.warehouse_id = 'WH_E01'`，前提是强制范围谓词仍在同一
  合取中——合取只能缩小结果集，故字面量永远无法放宽范围。字面量谓词本身出现在 OR / NOT 中同样判 WEAKENED。
- 收窄字面量的取值必须 ⊆ 授权范围（WAREHOUSE：authorized_warehouse_ids / region_id；REGION：region_id；
  NATIONAL：不校验）→ 否则 SCOPE_LITERAL_OUT_OF_SCOPE（纵深：意图阶段 _check_requested_scope 已先行拒答）。
  LIKE / GLOB / 函数包裹（lower(warehouse_id)）等无法静态求值的范围列比较一律按 SCOPE_LITERAL_OUT_OF_SCOPE 处理。

LIMIT 改写只作用于最外层查询；结果 normalized_sql 由 AST 重新生成（去注释、单语句、无末尾分号）。

error_hint 格式（回灌 repair_sql）：

    [SQL_GUARD_REJECTED] 共 {n} 处问题:
    1. [{code}] {message}
    2. ...
    可用表: {白名单表名列表} | 范围谓词: {render_scope_predicate 示例}

FATAL 违规的 message / error_hint 不得包含被拦截的敏感列名或禁止表名（避免向模型或日志确认其存在），统一使用
"引用了不允许访问的字段 / 表"。
"""

from __future__ import annotations

from agent_matrix.supply_chain_bi.models import (
    SqlViolationCode as V, FATAL_VIOLATION_CODES, RESERVED_BIND_PARAMS,
    ScopeLevel, SCOPE_REGION_PARAM, SCOPE_WAREHOUSES_PARAM,
)

from agent_matrix.supply_chain_bi.models import (
    SqlValidationResult,
    SqlViolation,
    WarehouseScopeFilter,
)
from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer

GUARD_HINT_CODE = "SQL_GUARD_REJECTED"

MASKED_LITERAL = "'***'"
"""mask_sql_for_display 对超过阈值的字符串字面量的替换文本。"""


class SqlStaticGuard:
    """无状态、纯函数式 SQL 静态守卫（线程 / 协程安全，可在 agent 内共享单例）。"""

    def __init__(self, semantic_layer: SemanticLayer, *, max_rows: int) -> None:
        """保存语义层与 LIMIT 上限；不做解析器预热以外的任何 I/O。"""
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        self.semantic_layer = semantic_layer
        self.max_rows = max_rows

    def validate(self, sql: str, *, scope: WarehouseScopeFilter) -> SqlValidationResult:
        """按模块文档 #1~#14 校验并规范化候选 SQL。

        - 永不抛异常：解析器内部异常一律转为 PARSE_ERROR 违规（CancelledError 不涉及，本方法为同步纯函数）。
        - 通过时 normalized_sql 已追加 / 收紧 LIMIT，referenced_tables / referenced_columns 已解析别名为物理表。
        - scope 只用于判定谓词形态（scope_level）与收窄字面量取值，**不**代入 SQL；取值仍由沙箱绑定参数注入。
        """
        from sqlglot import exp
        try:
            tree, violations = self._check_statement_shape(sql)
            if tree is not None:
                violations += self._check_tables(tree)
                violations += self._check_columns(tree)
                violations += self._check_functions_and_params(tree)
                violations += self._check_scope_predicates(tree, scope)
                try:
                    normalized, limit, rewritten = self._apply_limit(tree)
                except Exception:
                    violations.append(_violation(V.PARSE_ERROR, "LIMIT 必须为非负整数字面量，且不得携带 OFFSET"))
            violations = list({(v.code, v.message, v.table, v.column): v for v in violations}.values())
            fatal = any(v.code in FATAL_VIOLATION_CODES for v in violations)
            if violations:
                if fatal:
                    violations = [v for v in violations if v.code in FATAL_VIOLATION_CODES]
                    # Preserve statement-count evidence, but never disclose SQL fragments.
                    if tree is not None and getattr(tree, "meta", {}).get("multi"):
                        violations.insert(0, _violation(V.MULTI_STATEMENT, "只允许一条查询"))
                return SqlValidationResult(is_valid=False, fatal=fatal, violations=violations, original_sql="[REDACTED]" if fatal else mask_sql_for_display(sql), error_hint=self._render_error_hint(violations, scope))
            tables, columns = _references(tree, self.semantic_layer)
            return SqlValidationResult(is_valid=True, original_sql=mask_sql_for_display(sql), normalized_sql=normalized, referenced_tables=sorted(tables), referenced_columns={t: sorted(c) for t,c in sorted(columns.items())}, bind_params_used=sorted({p.name for p in tree.find_all(exp.Placeholder)}), limit_applied=limit, limit_rewritten=rewritten)
        except Exception:
            violations = [_violation(V.PARSE_ERROR, "SQL 无法解析，请改写为单条 SELECT")]
            return SqlValidationResult(is_valid=False, original_sql="[REDACTED]", violations=violations, error_hint=self._render_error_hint(violations, scope))

    # ---------------- 分项规则（validate 内部调用，单测可直接覆盖） ----------------

    def _check_statement_shape(self, sql: str) -> tuple[object | None, list[SqlViolation]]:
        """规则 #1~#4：解析、单语句、只读语句形态；返回 (AST 根节点或 None, 违规)。"""
        import sqlglot
        from sqlglot import exp
        try:
            trees = [t for t in sqlglot.parse(sql, read=self.semantic_layer.catalog.dialect.value, error_message_context=0, max_nodes=10000) if t is not None and not isinstance(t, exp.Semicolon)]
        except Exception:
            return None, [_violation(V.PARSE_ERROR, "SQL 无法解析，请检查语法")]
        if not trees:
            return None, [_violation(V.EMPTY_STATEMENT, "请提供 SELECT 查询")]
        violations = []
        if len(trees) != 1:
            violations.append(_violation(V.MULTI_STATEMENT, "只允许一条查询"))
            trees[0].meta["multi"] = True
        for tree in trees:
            if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                violations.append(_violation(V.NON_SELECT_STATEMENT, "只允许 SELECT 查询"))
            forbidden = {"insert", "update", "delete", "merge", "create", "drop", "alter", "command", "attach", "detach", "pragma", "vacuum", "into", "copy", "call", "set", "transaction", "commit", "rollback", "grant", "revoke", "replace", "execute", "use", "lock"}
            if any(n.key in forbidden for n in tree.walk()):
                violations.append(_violation(V.WRITE_OPERATION, "禁止写入或配置操作"))
        return trees[0], violations

    def _check_tables(self, tree: object) -> list[SqlViolation]:
        """规则 #5~#7：系统元数据表 / 禁止表 / 非白名单表；CTE 名与子查询别名不视为物理表。"""
        from sqlglot import exp
        violations = []
        for table in _physical_tables(tree):
            name = table.name.lower()
            full = ".".join(p.name for p in table.parts).lower()
            if self.semantic_layer.is_system_catalog(full):
                violations.append(_violation(V.SYSTEM_CATALOG_ACCESS, "引用了不允许访问的表"))
            elif name in self.semantic_layer.catalog.forbidden_tables:
                violations.append(_violation(V.FORBIDDEN_TABLE, "引用了不允许访问的表"))
            elif table.db or table.catalog or not isinstance(table.this, exp.Identifier) or not self.semantic_layer.get_table(name):
                violations.append(_violation(V.TABLE_NOT_WHITELISTED, "请仅使用语义目录内的物理表"))
        return violations

    def _check_columns(self, tree: object) -> list[SqlViolation]:
        """规则 #8~#10：解析列归属（限定名按别名映射；未限定列在单表 SELECT 中归属该表，多表时按白名单唯一匹配，
        无法唯一归属按 COLUMN_NOT_WHITELISTED 处理）；敏感列优先判定。"""
        from sqlglot import exp
        from sqlglot.optimizer.scope import traverse_scope
        violations = []
        # Include aliases as well: a sensitive field must not be echoed as a fabricated result name.
        physical_names = [t.name for t in _physical_tables(tree)]
        for identifier in tree.find_all(exp.Identifier):
            if self.semantic_layer.is_sensitive_column(None, identifier.name) or any(
                self.semantic_layer.is_sensitive_column(table, identifier.name) for table in physical_names
            ):
                violations.append(_violation(V.SENSITIVE_COLUMN, "引用了不允许访问的字段 / 表"))
        for star in tree.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                violations.append(_violation(V.SELECT_STAR_FORBIDDEN, "请显式列出需要的白名单列；COUNT(*) 除外"))
        for query_scope in traverse_scope(tree):
            for column in query_scope.columns:
                if column.is_star:
                    continue
                name = column.name.lower()
                sources = _column_sources(query_scope, column, self.semantic_layer)
                if any(self.semantic_layer.is_sensitive_column(t.name, name) for t in sources if isinstance(t, exp.Table)):
                    violations.append(_violation(V.SENSITIVE_COLUMN, "引用了不允许访问的字段 / 表"))
                    continue
                if self.semantic_layer.is_sensitive_column(None, name):
                    continue
                if len(sources) != 1:
                    allowed = sorted({c.name for s in query_scope.sources.values() if isinstance(s, exp.Table) for t in [self.semantic_layer.get_table(s.name)] if t for c in t.columns})
                    violations.append(_violation(V.COLUMN_NOT_WHITELISTED, "列不存在或归属不唯一；可用列: " + ", ".join(allowed), column=name))
        return violations

    def _check_functions_and_params(self, tree: object) -> list[SqlViolation]:
        """规则 #11~#12。"""
        from sqlglot import exp
        violations = []
        for func in tree.find_all(exp.Func):
            name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
            time_function = name in {"date", "time", "datetime", "julianday", "strftime", "unixepoch", "ts_or_ds_to_date", "ts_or_ds_to_timestamp"}
            rendered_name = func.sql(dialect=self.semantic_layer.catalog.dialect.value).partition("(")[0].lower()
            if {name, rendered_name} & set(self.semantic_layer.catalog.forbidden_functions) or (time_function and (not list(func.iter_expressions()) or any(v.is_string and v.this.lower() == "now" for v in func.find_all(exp.Literal)))):
                violations.append(_violation(V.FORBIDDEN_FUNCTION, "禁止危险或非确定性函数"))
        for param in tree.walk():
            if isinstance(param, (exp.Placeholder, exp.Parameter)) and (not isinstance(param, exp.Placeholder) or param.name not in RESERVED_BIND_PARAMS):
                violations.append(_violation(V.UNKNOWN_BIND_PARAM, "只允许 :scope_region_id / :scope_warehouse_ids / :as_of_date 命名参数"))
        return violations

    def _check_scope_predicates(self, tree: object, scope: WarehouseScopeFilter) -> list[SqlViolation]:
        """规则 #13（模块文档"范围谓词规则"）。"""
        from sqlglot import exp
        from sqlglot.optimizer.scope import traverse_scope
        if scope.scope_level is ScopeLevel.NATIONAL:
            return []
        violations = []
        for qs in traverse_scope(tree):
            select = qs.expression
            if not isinstance(select, exp.Select):
                continue
            for alias, (_, table) in qs.selected_sources.items():
                if not isinstance(table, exp.Table):
                    continue
                spec = self.semantic_layer.get_table(table.name)
                if not spec or not spec.requires_scope_filter:
                    continue
                roots = [select.args.get("where")]
                for join in select.args.get("joins", []):
                    if join.this is table and join.side.upper() == "LEFT":
                        roots.append(join.args.get("on"))
                terms = [t for root in roots if root is not None for t in _conjuncts(root.this if isinstance(root, exp.Where) else root)]
                def owns(c, name):
                    return isinstance(c, exp.Column) and c.name.lower() == name and (c.table.lower() == alias.lower() or (not c.table and len(qs.selected_sources) == 1))
                required = [(spec.region_column, SCOPE_REGION_PARAM)]
                if scope.scope_level is ScopeLevel.WAREHOUSE:
                    required.append((spec.warehouse_column, SCOPE_WAREHOUSES_PARAM))
                for colname, param in required:
                    def matches(term):
                        if param == SCOPE_REGION_PARAM and isinstance(term, exp.EQ):
                            return any(owns(c, colname) and isinstance(p, exp.Placeholder) and p.name == param for c,p in [(term.this,term.expression),(term.expression,term.this)])
                        if param == SCOPE_WAREHOUSES_PARAM and isinstance(term, exp.In) and owns(term.this, colname):
                            values = [term.args.get("field")] if term.args.get("field") is not None else term.expressions
                            return len(values) == 1 and isinstance(values[0], exp.Placeholder) and values[0].name == param
                        return False
                    if not any(matches(t) for t in terms):
                        local = [n for n in select.walk() if n.find_ancestor(exp.Select) is select]
                        weakened = any(matches(n) and n.find_ancestor(exp.Or, exp.Not, exp.Case) is not None for n in local)
                        violations.append(_violation(V.SCOPE_PREDICATE_WEAKENED if weakened else V.SCOPE_PREDICATE_MISSING, f"{alias} 缺少本层顶层 AND 合取的范围谓词", table=table.name.lower()))
                # Additional comparisons may narrow scope only through exact authorized literals.
                for node in select.walk():
                    if node.find_ancestor(exp.Select) is not select or not isinstance(node, (exp.Predicate, exp.Between)):
                        continue
                    cols = [c for c in node.find_all(exp.Column) if owns(c, spec.region_column) or owns(c, spec.warehouse_column)]
                    if not cols:
                        continue
                    if isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Column):
                        continue  # ordinary join key, never establishes authorization
                    if any(isinstance(x, exp.Placeholder) for x in node.walk()):
                        continue  # mandatory predicate shape is checked above
                    vals = []
                    left = node.this
                    if isinstance(node, exp.EQ):
                        other = node.expression
                        if isinstance(left, exp.Literal): left, other = other, left
                        if isinstance(other, exp.Literal) and other.is_string: vals = [other.this]
                    elif isinstance(node, exp.In) and node.expressions and all(isinstance(x, exp.Literal) and x.is_string for x in node.expressions):
                        vals = [x.this for x in node.expressions]
                    if not isinstance(left, exp.Column) or not vals:
                        violations.append(_violation(V.SCOPE_LITERAL_OUT_OF_SCOPE, "范围收窄仅允许授权编码的等值或 IN 字面量"))
                        continue
                    allowed = {scope.region_id} if left.name.lower() == spec.region_column else set(scope.authorized_warehouse_ids) if scope.scope_level is ScopeLevel.WAREHOUSE else None
                    if allowed is not None and not set(vals) <= allowed:
                        violations.append(_violation(V.SCOPE_LITERAL_OUT_OF_SCOPE, "收窄范围超出授权范围"))
                    if node not in terms:
                        violations.append(_violation(V.SCOPE_PREDICATE_WEAKENED, "范围收窄必须位于顶层 AND 合取"))
        return violations

    def _apply_limit(self, tree: object) -> tuple[str, int, bool]:
        """规则 #14：返回 (normalized_sql, limit_applied, limit_rewritten)。"""
        from sqlglot import exp
        limit = tree.args.get("limit")
        if tree.args.get("offset") is not None:
            raise ValueError("OFFSET is unsupported")
        value = limit.expression if limit else None
        if value is not None and (not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 0):
            raise ValueError("integer LIMIT required")
        applied = min(int(value.this), self.max_rows) if value is not None else self.max_rows
        rewritten = value is None or int(value.this) > self.max_rows
        tree = tree.copy().limit(applied)
        for node in tree.walk():
            node.comments = None
        return tree.sql(dialect=self.semantic_layer.catalog.dialect.value), applied, rewritten

    def _render_error_hint(self, violations: list[SqlViolation], scope: WarehouseScopeFilter) -> str:
        """按模块文档 error_hint 格式渲染；FATAL 违规文本脱敏。"""
        lines = [f"[{GUARD_HINT_CODE}] 共 {len(violations)} 处问题:"]
        lines += [f"{i}. [{v.code.value}] {v.message}" for i, v in enumerate(violations, 1)]
        if not any(v.code in FATAL_VIOLATION_CODES for v in violations):
            lines.append("可用表: " + ", ".join(t.table_name for t in self.semantic_layer.catalog.tables) + " | 范围谓词: " + self.semantic_layer.render_scope_predicate("i", scope.scope_level))
        return "\n".join(lines)


def mask_sql_for_display(normalized_sql: str) -> str:
    """生成响应 / 日志中展示的"脱敏 SQL"。

    - 输入必须是已通过守卫的 normalized_sql（故不含敏感列与禁止表）；
    - 绑定参数保持 `:scope_region_id` / `:scope_warehouse_ids` / `:as_of_date` 占位符，**不代入取值**；
    - 长度 > 32 的字符串字面量替换为 MASKED_LITERAL（防止模型把用户输入中的手机号等原样写入 SQL）；
    - 纯函数，输出稳定（同输入同输出）。
    """
    import sqlglot
    from sqlglot import exp
    try:
        tree = sqlglot.parse_one(normalized_sql, read="sqlite", error_message_context=0, max_nodes=10000)
        for node in tree.walk():
            node.comments = None
            if isinstance(node, exp.Literal) and node.is_string and len(node.this) > 32:
                node.set("this", "***")
        return tree.sql(dialect="sqlite")
    except Exception:
        return "[REDACTED]"


def _violation(code, message, **fields):
    return SqlViolation(code=code, message=message[:500], **fields)


def _physical_tables(tree):
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope
    tables = []
    for scope in traverse_scope(tree):
        tables.extend(source for _, source in scope.selected_sources.values() if isinstance(source, exp.Table))
    return tables


def _column_sources(scope, column, semantic):
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope
    name = column.name.lower()
    sources = {a.lower(): s for a, (_, s) in scope.selected_sources.items()}
    candidates = [sources[column.table.lower()]] if column.table and column.table.lower() in sources else list(sources.values()) if not column.table else []
    matches = []
    for source in candidates:
        if isinstance(source, exp.Table):
            spec = semantic.get_table(source.name)
            if spec and name in spec.allowed_columns:
                matches.append(source)
        elif isinstance(source, Scope):
            names = source.outer_columns or source.expression.named_selects
            if name in [n.lower() for n in names]:
                matches.append(source)
    # Correlated columns resolve against their lexical parent, never sibling scopes.
    if not matches and scope.parent and (column.table and column.table.lower() not in sources):
        return _column_sources(scope.parent, column, semantic)
    return matches


def _references(tree, semantic):
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope
    tables = {t.name.lower() for t in _physical_tables(tree)}
    columns = {t: set() for t in tables}
    for scope in traverse_scope(tree):
        for col in scope.columns:
            for source in _column_sources(scope, col, semantic):
                if isinstance(source, exp.Table):
                    columns[source.name.lower()].add(col.name.lower())
    return tables, columns


def _conjuncts(node):
    from sqlglot import exp
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]
