"""Acceptance #87–89 and automated frozen-contract audit."""
import ast
import asyncio
from pathlib import Path
import re
import pytest
from agent_matrix.finance_audit.agent import AUDIT_RENDER_VERSION
from agent_matrix.finance_audit.api import create_app
from agent_matrix.finance_audit.models import FinanceRuleConfig

ROOT=Path(__file__).resolve().parents[3]
PROTECTED=('data_platform','eval_platform','agent_matrix/store_sop','agent_matrix/supply_chain_bi','mock_docs')


async def git(*args):
    process=await asyncio.create_subprocess_exec('git',*args,cwd=ROOT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await process.communicate()
    assert process.returncode==0,err.decode()
    return out.decode()


async def test_87_upstream_modules_zero_diff():
    assert not await git('diff','--stat','main','--',*PROTECTED)
    assert not await git('ls-files','--others','--exclude-standard','--',*PROTECTED)


async def test_frozen_models_ports_and_signatures():
    base=ROOT/'agent_matrix/finance_audit'
    for name in ('models.py','ports.py'):
        assert (await asyncio.to_thread((base/name).read_text))==await git('show','main:agent_matrix/finance_audit/'+name)
    def signatures(source):
        tree=ast.parse(source); result={}
        def collect(nodes,prefix=''):
            for node in nodes:
                if isinstance(node,ast.ClassDef): collect(node.body,prefix+node.name+'.')
                elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    result[prefix+node.name]=(ast.dump(node.args),ast.dump(node.returns) if node.returns else None,[ast.dump(d) for d in node.decorator_list])
        collect(tree.body)
        return result
    for name in ('agent.py','api.py','errors.py','redaction.py','rule_engine.py','eval_judge.py'):
        before=signatures(await git('show','main:agent_matrix/finance_audit/'+name))
        after=signatures(await asyncio.to_thread((base/name).read_text))
        assert all(after.get(k)==value for k,value in before.items()),name


def test_88_no_v1_or_binary_float_money():
    for path in (ROOT/'agent_matrix').rglob('*.py'):
        text=path.read_text()
        assert '.dict()' not in text and '@validator' not in text,path
    for path in (ROOT/'agent_matrix/finance_audit').glob('*.py'):
        text=path.read_text()
        assert not re.search(r'\b(?:float|round)\s*\(',text),path
        assert not re.search(r'Decimal\(\s*\d+\.\d+',text),path
        assert 'math.' not in text,path
        tree=ast.parse(text)
        assert not any(isinstance(n,ast.Raise) and isinstance(n.exc,ast.Name) and n.exc.id=='NotImplementedError' for n in ast.walk(tree))


def test_89_workflow_versions_and_operations():
    workflow=(ROOT/'agent_matrix/workflows/finance_audit_workflow.yml').read_text()
    for path,methods in create_app().openapi()['paths'].items():
        assert path in workflow
        for operation in methods.values(): assert operation['operationId'] in workflow
    assert f'value: "{AUDIT_RENDER_VERSION}"' in workflow
    assert f'value: "{FinanceRuleConfig().ruleset_version}"' in workflow
