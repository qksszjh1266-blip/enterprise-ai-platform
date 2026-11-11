"""存储同步层异常体系。

所有异常都携带面向 Agent / 调用方的结构化修正提示（to_hint），
上层 FastAPI / MCP 适配层捕获后转换为结构化响应，禁止裸抛 500。
"""

from __future__ import annotations


class StorageSyncError(Exception):
    """同步层异常基类。"""

    code: str = "STORAGE_SYNC_ERROR"
    suggestion: str = "请检查存储状态后重试。"

    def __init__(self, message: str, *, doc_id: str | None = None, version: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.doc_id = doc_id
        self.version = version

    def to_hint(self) -> str:
        """生成结构化修正提示文本（格式：[code] doc_id=... version=... message | 建议）。"""
        return f"[{self.code}] doc_id={self.doc_id if self.doc_id is not None else '-'} version={self.version if self.version is not None else '-'} | {self.message} | 建议: {self.suggestion}"


class ShadowWriteError(StorageSyncError):
    """v+1 旁路写入（PG 父块 / 向量 / ES 任一端）失败；已触发影子版本回滚。"""

    code = "SHADOW_WRITE_FAILED"
    suggestion = "已回滚 v+1，当前版本仍可用，请检查下游存储连通性后重试。"


class VersionConflictError(StorageSyncError):
    """current_version CAS 切换失败：期间有其他写者已推进版本。"""

    code = "VERSION_CONFLICT"
    suggestion = "文档已被并发更新，请重新拉取最新内容后重试。"


class RollbackError(StorageSyncError):
    """影子版本回滚本身失败，存在残留脏数据，需人工或下次 GC 兜底。"""

    code = "ROLLBACK_FAILED"
    suggestion = "存在影子残留，将由下一次 GC 清理；如持续出现请人工介入。"


class GarbageCollectionError(StorageSyncError):
    """旧版本异步 GC 失败（不影响已发布版本的可用性）。"""

    code = "GC_FAILED"
    suggestion = "不影响检索，将在下一次同步时重试清理。"
