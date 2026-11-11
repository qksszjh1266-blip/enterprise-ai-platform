"""Public interfaces, prompt constants and upstream compatibility checks."""
import ast
from pathlib import Path
from tests.eval_platform.test_contracts import original, methods

ROOT=Path(__file__).resolve().parents[3]


def test_public_contracts_and_upstream_code():
    for directory in ('data_platform','eval_platform'):
        for path in (ROOT/directory).rglob('*.py'):
            assert path.read_text()==original(str(path.relative_to(ROOT)))
    for filename in ('models.py','ports.py'):
        path='agent_matrix/store_sop/'+filename
        assert (ROOT/path).read_text()==original(path)
    for filename in ('agent.py','api.py','prompts.py','errors.py'):
        path='agent_matrix/store_sop/'+filename
        before=methods(ast.parse(original(path)))
        after=methods(ast.parse((ROOT/path).read_text()))
        for key,node in before.items():
            updated=after[key]
            assert type(node) is type(updated)
            assert ast.dump(node.args)==ast.dump(updated.args),key
            assert (ast.dump(node.returns) if node.returns is not None else None)==(
                ast.dump(updated.returns) if updated.returns is not None else None),key
            assert [ast.dump(d) for d in node.decorator_list]==[ast.dump(d) for d in updated.decorator_list],key


def test_only_four_model_call_sites_and_no_stubs_or_v1():
    source=(ROOT/'agent_matrix/store_sop/agent.py').read_text()
    sites=[]
    for method in methods(ast.parse(source)).values():
        for node in ast.walk(method):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and ast.unparse(node.func.value)=='self.chat_model':
                sites.append(method.name)
    assert sorted(sites)==['_classify_and_extract','_decide_next_action','_generate_grounded_answer','_maybe_summarize']
    for path in (ROOT/'agent_matrix/store_sop').glob('*.py'):
        text=path.read_text()
        assert '.dict()' not in text and '@validator' not in text
        assert 'raise NotImplementedError' not in text


def test_prompt_constants_unchanged():
    def constants(source):
        result={}
        for node in ast.parse(source).body:
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                names=[target.id for target in node.targets if isinstance(target,ast.Name)] if isinstance(node,ast.Assign) else [node.target.id]
                for name in names:
                    if name.isupper(): result[name]=ast.dump(node.value)
        return result
    path='agent_matrix/store_sop/prompts.py'
    assert constants(original(path))==constants((ROOT/path).read_text())
