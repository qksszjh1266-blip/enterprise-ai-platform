import pytest
from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.storage_sync.models import ChunkingResult


def chunk(markdown, **kwargs):
    return HierarchicalMarkdownChunker(**kwargs).chunk(doc_id='doc', version=1, markdown=markdown, dept_id='ops', allowed_roles=['staff'])


def test_heading_paths():
    result = chunk('# A\na\n## B\nb\n### C\nc')
    assert [p.section_path for p in result.parents] == ['A', 'A/B', 'A/B/C']
    assert [p.full_content for p in result.parents] == ['A\na', 'B\nb', 'C\nc']


@pytest.mark.parametrize('fence', ['```', '~~~~'])
def test_fenced_heading(fence):
    result = chunk(f'# A\n{fence}python\n# not heading\n\n## neither\n{fence}\nend')
    assert len(result.parents) == 1
    assert '# not heading' in result.parents[0].full_content


def test_page_and_preamble():
    result = chunk('前言\n<!-- page: 3 -->\n# A\n正文\n#### 正文标题\n其他')
    assert result.parents[0].section_path == ''
    assert result.parents[1].page_number == 3
    assert '#### 正文标题' in result.parents[1].full_content
    assert '<!--' not in result.parents[1].full_content


def test_large_parents_atomic_blocks():
    table = '| 名称 | 数值 |\n| --- | --- |\n| 纸箱 | 20 |'
    code = '```\n# example\n\nprint(1)\n```'
    result = chunk('# A\n' + ('规格参数。'*10+'\n\n')*10 + table+'\n\n'+code, parent_max_chars=100, child_max_chars=40, child_overlap_chars=8)
    assert all(len(p.full_content) <= 100 for p in result.parents)
    assert sum(table in p.full_content for p in result.parents) == 1
    assert sum(code in p.full_content for p in result.parents) == 1
    huge = '| ' + 'x'*200 + ' |'
    assert huge in chunk('# A\n'+huge, parent_max_chars=100, child_max_chars=40, child_overlap_chars=8).parents[0].full_content


def test_children_overlap_ids_and_acl():
    result = chunk('# A\n'+'abcdef。'*60, child_max_chars=50, child_overlap_chars=10)
    children = result.children
    pieces = [c.child_text.removeprefix('A\n') for c in children]
    assert all(len(p) <= 50 for p in pieces)
    assert all(a[-10:] == b[:10] for a,b in zip(pieces, pieces[1:]))
    assert all(c.parent_id in {p.parent_id for p in result.parents} for c in children)
    assert all(c.allowed_roles == ['staff'] and c.dept_id == 'ops' for c in children)
    assert [c.chunk_index for c in children] == list(range(len(children)))


def test_deterministic():
    assert chunk('# A\n正文') == chunk('# A\n正文')
    assert HierarchicalMarkdownChunker.make_parent_id('doc', 2, 3) == 'doc:v2:p0003'
    assert HierarchicalMarkdownChunker.make_child_id('p', 1) == 'p:c001'


@pytest.mark.parametrize('kwargs', [{'child_overlap_chars':400}, {'child_overlap_chars':-1}, {'parent_heading_levels':()}, {'parent_heading_levels':(0,7)}, {'parent_max_chars':100}])
def test_invalid(kwargs):
    with pytest.raises(ValueError):
        HierarchicalMarkdownChunker(**kwargs)


@pytest.mark.parametrize('text', ['', '  \n\n', '# A\n\n'])
def test_empty(text):
    assert chunk(text) == ChunkingResult(parents=[], children=[])
