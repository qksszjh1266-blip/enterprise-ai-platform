"""Public contract and data platform compatibility checks."""
import ast
from pathlib import Path
import subprocess
from tests.contract_baseline import assert_python_tree_frozen, assert_structure_frozen, baseline_files, current_files, parse_normalized

ROOT = Path(__file__).resolve().parents[2]


def original(path):
    return subprocess.check_output(['git','show',f'main:{path}'],cwd=ROOT,text=True)


def methods(tree):
    output={}
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            output[node.name]=node
        elif isinstance(node,ast.ClassDef):
            for member in node.body:
                if isinstance(member,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    output[f'{node.name}.{member.name}']=member
    return output


def test_data_platform_unchanged_from_main():
    # Structure frozen against main; only non-ASCII scenario copy (docstrings / descriptions) may differ.
    assert_python_tree_frozen('data_platform')
    assert [p for p in baseline_files('data_platform') if not p.endswith('.py')]==[p for p in current_files('data_platform') if not p.endswith('.py')]


def test_frozen_eval_signatures_models_and_validators():
    for name in ('models.py','ports.py'):
        assert_structure_frozen(f'eval_platform/{name}')
    for name in ('evaluators.py','runner.py','report_generator.py'):
        path=f'eval_platform/{name}'
        before=methods(parse_normalized(original(path)))
        after=methods(parse_normalized((ROOT/path).read_text()))
        for key,node in before.items():
            replacement=after[key]
            assert type(node) is type(replacement),key
            assert ast.dump(node.args)==ast.dump(replacement.args),key
            # Unannotated helpers legitimately have returns=None; ast.dump only accepts ASTs.
            assert (ast.dump(node.returns) if node.returns is not None else None)==(
                ast.dump(replacement.returns) if replacement.returns is not None else None),key
            assert [ast.dump(d) for d in node.decorator_list]==[ast.dump(d) for d in replacement.decorator_list],key


def test_no_v1_syntax_or_unimplemented_methods():
    for path in (ROOT/'eval_platform').rglob('*.py'):
        source=path.read_text()
        assert '.dict()' not in source and '@validator' not in source
        assert 'raise NotImplementedError' not in source
