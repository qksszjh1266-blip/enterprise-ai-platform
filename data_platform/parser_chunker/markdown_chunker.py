"""层级 Markdown 父子块切分器（骨架）。

切分策略：
- 父块（Parent）：按 Markdown 标题层级（# / ## / ###）切分出语义完整的章节，
  存入 PG kb_parent_chunks，用于召回后回填完整上下文给 LLM。
- 子块（Child）：在父块内按字符窗口 + 重叠切分，写入 Milvus / ES 做召回。
- ID 确定性：parent_id / child_id 由 (doc_id, version, 序号) 推导，保证重试幂等。

本模块为纯 CPU 计算，不涉及 I/O，因此为同步方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from data_platform.storage_sync.models import (
    ChildChunkPayload,
    ChunkingResult,
    ParentChunkSchema,
)


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    """切分中间结构：一个标题章节。"""

    heading_path: tuple[str, ...]
    body: str
    page_number: int | None = None
    level: int = 0


@dataclass(slots=True)
class HierarchicalMarkdownChunker:
    """层级 Markdown 父子块切分器。

    参数：
        parent_heading_levels: 作为父块边界的标题级别，默认 (1, 2, 3)。
        parent_max_chars: 父块最大字符数，超长章节按段落二次切分。
        child_max_chars: 子块窗口字符数。
        child_overlap_chars: 相邻子块重叠字符数，须 < child_max_chars。
        section_separator: section_path 拼接分隔符。
    """

    parent_heading_levels: tuple[int, ...] = field(default=(1, 2, 3))
    parent_max_chars: int = 2000
    child_max_chars: int = 400
    child_overlap_chars: int = 50
    section_separator: str = "/"

    def __post_init__(self) -> None:
        """校验参数合法性：overlap < child_max_chars <= parent_max_chars，级别在 1..6，否则抛 ValueError。"""
        if not (0 <= self.child_overlap_chars < self.child_max_chars <= self.parent_max_chars):
            raise ValueError("Invalid chunk sizes")
        if not self.parent_heading_levels or not set(self.parent_heading_levels) <= set(range(1, 7)):
            raise ValueError("Invalid heading levels")

    def chunk(
        self,
        *,
        doc_id: str,
        version: int,
        markdown: str,
        dept_id: str,
        allowed_roles: list[str],
    ) -> ChunkingResult:
        """切分入口：Markdown 原文 -> 父块列表 + 子块列表。

        流程：split_sections -> build_parents -> 逐父块 split_children。
        子块继承 doc_id / version / dept_id / allowed_roles（ACL 标量字段）。
        空文档返回空 ChunkingResult，不抛异常。
        """
        parents = self.build_parents(doc_id=doc_id, version=version, sections=self.split_sections(markdown))
        return ChunkingResult(parents=parents, children=[child for parent in parents for child in self.split_children(parent, dept_id=dept_id, allowed_roles=allowed_roles)])

    def split_sections(self, markdown: str) -> list[MarkdownSection]:
        """按标题层级解析章节。

        - 识别 ATX 标题（# ~ ######），忽略围栏代码块（```）内的 '#'；
        - 维护标题栈生成 heading_path；
        - 识别分页标记 <!-- page: N --> 回填 page_number；
        - 首个标题前的正文作为 heading_path=() 的前言章节。
        """
        sections = []
        stack = []
        lines = []
        page = None
        section_page = None
        fence = None
        def flush():
            if lines:
                sections.append(MarkdownSection(tuple(title for _, title in stack), "\n".join(lines), section_page, stack[-1][0] if stack else 0))
                lines.clear()
        for line in markdown.splitlines():
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if fence:
                lines.append(line)
                if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                    fence = None
                continue
            if marker:
                fence = marker[1]
                lines.append(line)
                continue
            pagination = re.fullmatch(r"\s*<!--\s*page:\s*([1-9]\d*)\s*-->\s*", line)
            if pagination:
                page = int(pagination[1])
                if not any(x.strip() for x in lines):
                    section_page = page
                continue
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading and len(heading[1]) in self.parent_heading_levels:
                flush()
                level = len(heading[1])
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, heading[2]))
                section_page = page
            else:
                if not lines:
                    section_page = page
                lines.append(line)
        flush()
        return sections

    def build_parents(
        self, *, doc_id: str, version: int, sections: list[MarkdownSection]
    ) -> list[ParentChunkSchema]:
        """章节 -> 父块。

        - 跳过空正文章节（但其标题仍进入子章节的 heading_path）；
        - 超过 parent_max_chars 的章节按空行段落边界二次切分，不得截断表格行与代码块；
        - full_content 前置标题行，保证父块自描述；
        - parent_id 由 make_parent_id 生成。
        """
        parents = []
        for section in sections:
            if not section.body.strip():
                continue
            prefix = section.heading_path[-1] + "\n" if section.heading_path else ""
            budget = max(1, self.parent_max_chars - len(prefix))
            # Atomic fenced blocks and contiguous table rows survive paragraph splitting.
            blocks = []
            ordinary = []
            atomic = []
            fence = None
            def flush_ordinary():
                if ordinary:
                    text = "\n".join(ordinary).strip()
                    if text:
                        blocks.extend(text[i:i+budget] for i in range(0, len(text), budget))
                    ordinary.clear()
            for line in section.body.splitlines():
                marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
                if fence:
                    atomic.append(line)
                    if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                        blocks.append("\n".join(atomic)); atomic.clear(); fence = None
                elif marker:
                    flush_ordinary()
                    if atomic:
                        blocks.append("\n".join(atomic)); atomic.clear()
                    fence = marker[1]; atomic.append(line)
                elif line.lstrip().startswith("|"):
                    flush_ordinary(); atomic.append(line)
                else:
                    if atomic:
                        blocks.append("\n".join(atomic)); atomic.clear()
                    if not line.strip():
                        flush_ordinary()
                    else:
                        ordinary.append(line)
            flush_ordinary()
            if atomic:
                blocks.append("\n".join(atomic))
            pieces = []
            current = ""
            for block in blocks:
                candidate = current + "\n\n" + block if current else block
                if current and len(candidate) > budget:
                    pieces.append(current); current = block
                else:
                    current = candidate
            if current:
                pieces.append(current)
            for piece in pieces:
                parents.append(ParentChunkSchema(parent_id=self.make_parent_id(doc_id, version, len(parents)), doc_id=doc_id, version=version, section_path=self.section_separator.join(section.heading_path), full_content=prefix+piece, page_number=section.page_number))
        return parents

    def split_children(
        self, parent: ParentChunkSchema, *, dept_id: str, allowed_roles: list[str]
    ) -> list[ChildChunkPayload]:
        """父块 -> 子块（滑动窗口 + 重叠）。

        - 优先在句末标点（。！？；.!?\\n）处断开，找不到时硬切；
        - 子块文本前缀 section_path，提升 BM25/向量命中；
        - 过滤 strip 后为空的子块；embedding 置 None 由同步管理器回填。
        """
        children = []
        text = parent.full_content
        start = 0
        while start < len(text):
            end = min(start + self.child_max_chars, len(text))
            if end < len(text):
                lower = max(start + self.child_overlap_chars + 1, start + int(self.child_max_chars * .7))
                for pos in range(end - 1, lower - 1, -1):
                    if text[pos] in "。！？；.!?\n":
                        end = pos + 1
                        break
            piece = text[start:end]
            if piece.strip():
                index = len(children)
                children.append(ChildChunkPayload(child_id=self.make_child_id(parent.parent_id, index), parent_id=parent.parent_id, doc_id=parent.doc_id, version=parent.version, dept_id=dept_id, allowed_roles=list(allowed_roles), child_text=(parent.section_path+"\n" if parent.section_path else "")+piece, chunk_index=index))
            if end == len(text):
                break
            start = max(start + 1, end - self.child_overlap_chars)
        return children

    @staticmethod
    def make_parent_id(doc_id: str, version: int, ordinal: int) -> str:
        """确定性父块 ID：'{doc_id}:v{version}:p{ordinal:04d}'。"""
        return f"{doc_id}:v{version}:p{ordinal:04d}"

    @staticmethod
    def make_child_id(parent_id: str, index: int) -> str:
        """确定性子块 ID：'{parent_id}:c{index:03d}'。"""
        return f"{parent_id}:c{index:03d}"
