import ast
from pathlib import Path
import re
from agent_matrix.supply_chain_bi.api import create_app,API_PREFIX
from agent_matrix.supply_chain_bi.render import RENDER_VERSION
from agent_matrix.supply_chain_bi.semantic_layer import SUPPLY_CHAIN_CATALOG
from tests.contract_baseline import _Normalize, assert_python_tree_frozen, baseline_source

ROOT=Path(__file__).resolve().parents[3]
PROTECTED=['data_platform/','eval_platform/','agent_matrix/store_sop/']

def test_50_upstream_modules_zero_diff():
    assert_python_tree_frozen(*PROTECTED)

def test_51_async_pydantic_compliance():
    for path in (ROOT/'agent_matrix').rglob('*.py'):
        text=path.read_text()
        assert not re.search(r'\.dict\(\)|@validator|sqlite3\.connect|import sqlite3',text),path

def test_52_workflow_consistency():
    text=(ROOT/'agent_matrix/workflows/supply_chain_bi_workflow.yml').read_text()
    schema=create_app().openapi()
    for path,methods in schema['paths'].items():
        assert path in text
        for spec in methods.values():assert spec['operationId'] in text
    assert f'value: "{RENDER_VERSION}"' in text
    assert f'value: {SUPPLY_CHAIN_CATALOG.catalog_version}' in text

def test_frozen_contracts_unchanged():
    folder=ROOT/'agent_matrix/supply_chain_bi'
    for path in folder.glob('*.py'):
        if path.name=='sandbox_sqlite.py':continue
        baseline=baseline_source(f'agent_matrix/supply_chain_bi/{path.name}')
        old,new=(_Normalize({}).visit(ast.parse(source)) for source in (baseline,path.read_text()))
        def declarations(tree):
            return {node.name:node for node in tree.body if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))}
        old_nodes,new_nodes=declarations(old),declarations(new)
        for name,node in old_nodes.items():
            assert name in new_nodes
            if isinstance(node,ast.ClassDef):
                before,after=declarations(node),declarations(new_nodes[name])
                for method,fn in before.items():
                    assert ast.dump(fn.args)==ast.dump(after[method].args),(path,method)
                    assert (ast.dump(fn.returns) if fn.returns else None) == (ast.dump(after[method].returns) if after[method].returns else None)
                    if method.startswith('_check_') and path.name=='models.py':assert ast.dump(fn)==ast.dump(after[method])
                fields=lambda c:[ast.dump(n) for n in c.body if isinstance(n,ast.AnnAssign)]
                assert fields(node)==fields(new_nodes[name])
            else:assert ast.dump(node.args)==ast.dump(new_nodes[name].args)
