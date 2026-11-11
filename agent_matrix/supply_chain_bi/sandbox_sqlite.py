"""File-backed, independently read-only SQLite adapter; no database creation here."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from time import monotonic, perf_counter

import aiosqlite

from .errors import SandboxUnavailableError
from .models import ResultColumn, ScalarValue, SqlDialect, SqlExecutionResult, SqlExecutionStatus


class SqliteReadOnlySandbox:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    @property
    def dialect(self) -> SqlDialect:
        return SqlDialect.SQLITE

    async def execute(
        self, *, sql: str, bind_params: Mapping[str, ScalarValue | Sequence[str]],
        timeout_s: float, max_rows: int,
    ) -> SqlExecutionResult:
        started = perf_counter()
        db = None
        try:
            db = await aiosqlite.connect(self.path.as_uri() + '?mode=ro', uri=True)
            await db.execute('PRAGMA query_only=ON')
        except Exception as exc:
            if db is not None:
                await db.close()
            raise SandboxUnavailableError('只读数据源连接失败') from exc
        try:
            deadline = monotonic() + timeout_s
            await db.set_progress_handler(lambda: int(monotonic() >= deadline), 100)
            # The independent authorizer also prevents ATTACH and disabling query_only.
            # Numeric opcodes are SQLite's stable public authorizer API constants.
            denied = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                      18, 19, 22, 23, 24, 25, 26, 27, 28, 29, 30}
            await db.set_authorizer(lambda action, a, b, database, trigger: 1 if action in denied or (action == 31 and str(b).lower() in {'load_extension', 'readfile', 'writefile'}) else 0)
            sql, params = _expand(sql, bind_params)
            async with asyncio.timeout(timeout_s):
                async with db.execute(sql, params) as cursor:
                    rows = await cursor.fetchmany(max_rows + 1)
                    columns = [ResultColumn(name=c[0]) for c in cursor.description or []]
            return SqlExecutionResult(status=SqlExecutionStatus.SUCCESS, columns=columns,
                rows=[list(r) for r in rows[:max_rows]], row_count=min(len(rows), max_rows),
                truncated=len(rows) > max_rows, elapsed_ms=(perf_counter()-started)*1000)
        except asyncio.CancelledError:
            await db.interrupt()
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                await db.interrupt()
            message = str(exc)
            status = SqlExecutionStatus.RUNTIME_ERROR
            hint = '查询无法执行，请简化查询并检查表达式。'
            if isinstance(exc, TimeoutError) or 'interrupted' in message:
                status, hint = SqlExecutionStatus.TIMEOUT, '查询超时，请缩小时间范围或减少关联。'
            elif any(t in message.lower() for t in ('readonly', 'read-only', 'not authorized', 'authorization denied')):
                status, hint = SqlExecutionStatus.READ_ONLY_VIOLATION, '只读安全策略拒绝执行。'
            elif 'no such column:' in message:
                status, hint = SqlExecutionStatus.UNKNOWN_COLUMN, '未知列: ' + _safe_identifier(message.split('no such column:', 1)[1])
            elif 'no such table:' in message:
                status, hint = SqlExecutionStatus.UNKNOWN_TABLE, '未知表，请使用语义目录中的表。'
            elif 'syntax error' in message or 'incomplete input' in message:
                status, hint = SqlExecutionStatus.SYNTAX_ERROR, 'SQL 语法错误，请按 SQLite 方言修正。'
            elif any(t in message for t in ('no such function', 'wrong number', 'datatype', 'binding', 'type')):
                status, hint = SqlExecutionStatus.TYPE_ERROR, '函数或参数类型不兼容，请按 SQLite 方言修正。'
            return SqlExecutionResult(status=status, error_code=status.value.upper(),
                error_hint=f'[{status.value.upper()}] {hint}', elapsed_ms=(perf_counter()-started)*1000)
        finally:
            await db.close()

    async def latest_snapshot_date(self) -> date:
        result = await self.execute(sql='SELECT MAX(snapshot_date) AS snapshot_date FROM fact_warehouse_inventory_daily LIMIT 1', bind_params={}, timeout_s=5, max_rows=1)
        try:
            if result.status is not SqlExecutionStatus.SUCCESS:
                raise ValueError('snapshot unavailable')
            return date.fromisoformat(result.rows[0][0])
        except (ValueError, IndexError, TypeError) as exc:
            raise SandboxUnavailableError('无法读取最新快照日期') from exc


def _safe_identifier(value: str) -> str:
    import re
    from .semantic_layer import SUPPLY_CHAIN_CATALOG
    value = value.strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]{0,127}', value) or any(c in value.lower() for c in SUPPLY_CHAIN_CATALOG.global_sensitive_columns):
        return '请检查语义目录中的列名'
    return value


def _expand(sql, bindings):
    """Token-aware expansion: never substitute placeholders inside strings/comments."""
    import sqlglot
    from sqlglot import exp
    # Preserve raw SQL for the independent read-only defense (including writes).
    if not any(isinstance(v, Sequence) and not isinstance(v, str) for v in bindings.values()):
        return sql, dict(bindings)
    tree = sqlglot.parse_one(sql, read='sqlite')
    params = {k: v for k,v in bindings.items() if not isinstance(v, Sequence) or isinstance(v, str)}
    for node in list(tree.find_all(exp.In)):
        values = [node.args['field']] if node.args.get('field') is not None else node.expressions
        if len(values) != 1 or not isinstance(values[0], exp.Placeholder):
            continue
        name = values[0].name
        bound = bindings.get(name)
        if isinstance(bound, Sequence) and not isinstance(bound, str):
            placeholders = []
            for i, value in enumerate(bound):
                key = f'{name}_{i}'
                params[key] = value
                placeholders.append(exp.Placeholder(this=key))
            node.set('field', None)
            node.set('expressions', placeholders or [exp.Null()])
    return tree.sql(dialect='sqlite'), params
