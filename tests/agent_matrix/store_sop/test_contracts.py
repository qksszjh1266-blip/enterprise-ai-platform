"""Public interfaces, prompt constants and upstream compatibility checks."""
import ast
from pathlib import Path
from tests.contract_baseline import assert_python_tree_frozen, assert_structure_frozen, format_fields, parse_normalized, string_set, structure
from tests.eval_platform.test_contracts import original, methods

ROOT=Path(__file__).resolve().parents[3]


def test_public_contracts_and_upstream_code():
    assert_python_tree_frozen('data_platform','eval_platform')
    for filename in ('models.py','ports.py'):
        assert_structure_frozen('agent_matrix/store_sop/'+filename)
    for filename in ('agent.py','api.py','prompts.py','errors.py'):
        path='agent_matrix/store_sop/'+filename
        before=methods(parse_normalized(original(path)))
        after=methods(parse_normalized((ROOT/path).read_text()))
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
    # Same constant set, same structure and ASCII literals, same str.format placeholders;
    # only the Chinese copy may be re-skinned, and only together with a PROMPT_VERSION bump.
    def constants(source):
        result={}
        for node in ast.parse(source).body:
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                names=[target.id for target in node.targets if isinstance(target,ast.Name)] if isinstance(node,ast.Assign) else [node.target.id]
                for name in names:
                    if name.isupper(): result[name]=node.value
        return result
    def placeholders(node):
        return sorted(f for n in ast.walk(node) if isinstance(n,ast.Constant) and isinstance(n.value,str) for f in format_fields(n.value))
    path='agent_matrix/store_sop/prompts.py'
    before,after=constants(original(path)),constants((ROOT/path).read_text())
    assert before.keys()==after.keys()
    for name in before:
        if name=='PROMPT_VERSION': continue
        if name=='SAFETY_HAZARD_KEYWORDS':
            # Safety triggers may only be extended, never removed.
            assert string_set(original(path),name)<=string_set((ROOT/path).read_text(),name)
            continue
        assert structure(ast.unparse(before[name]))==structure(ast.unparse(after[name])),name
        assert placeholders(before[name])==placeholders(after[name]),name
    assert_structure_frozen(path)
