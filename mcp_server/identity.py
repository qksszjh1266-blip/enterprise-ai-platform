"""MCP 调用方可信身份。

检索工具的 department / role 入参由 LLM 生成，不可信；它们只能用来"收窄"可信身份的权限，
永远不能扩大权限。可信身份 CallerPrincipal 在 Server 启动时由宿主注入（stdio 模式读取环境变量，
生产环境应由网关 / JWT 解析后注入），工具调用时逐次校验：

    department == principal.department  AND  role ∈ principal.roles

校验通过后才以 UserContext(dept_id=department, roles=[role]) 调用 HybridRetrievalService，
后者再执行原有的台账 ACL 前置过滤（双重校验，任何一层不通过都拿不到文档）。

环境变量（均为可选；未设置时回落到最低权限的电商运营专员演示身份）：

    EAP_MCP_USER_ID      调用用户 ID（审计用）
    EAP_MCP_DEPARTMENT   所属部门，如 store_ops
    EAP_MCP_ROLES        逗号分隔的角色列表，如 clerk,store_manager
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
"""部门 / 角色标识符格式：小写字母开头，仅含小写字母、数字与下划线。"""

ENV_USER_ID = "EAP_MCP_USER_ID"
ENV_DEPARTMENT = "EAP_MCP_DEPARTMENT"
ENV_ROLES = "EAP_MCP_ROLES"

DEMO_USER_ID = "mcp-demo-clerk"
DEMO_DEPARTMENT = "store_ops"
DEMO_ROLES = ("clerk",)


class PrincipalConfigError(ValueError):
    """可信身份配置无效（启动期错误，直接终止启动，不做降级）。"""


class CallerPrincipal(BaseModel):
    """宿主注入的可信调用方身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    user_id: str = Field(..., min_length=1, max_length=128)
    department: str = Field(..., pattern=IDENTIFIER_PATTERN)
    roles: tuple[str, ...] = Field(..., min_length=1)

    @field_validator("roles")
    @classmethod
    def _check_roles(cls, roles: tuple[str, ...]) -> tuple[str, ...]:
        bad = [r for r in roles if not re.fullmatch(IDENTIFIER_PATTERN, r)]
        if bad:
            raise ValueError(f"角色标识符格式无效: {bad}")
        return roles

    def permits(self, *, department: str, role: str) -> bool:
        """入参部门与角色是否落在可信身份的权限范围内。"""
        return department == self.department and role in self.roles

    @classmethod
    def demo(cls) -> CallerPrincipal:
        """最低权限演示身份：电商运营部运营专员。"""
        return cls(user_id=DEMO_USER_ID, department=DEMO_DEPARTMENT, roles=DEMO_ROLES)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CallerPrincipal:
        """从环境变量读取身份；部门与角色须同时设置或同时缺省，缺省时使用 demo()。"""
        env = os.environ if environ is None else environ
        department = env.get(ENV_DEPARTMENT, "").strip()
        roles_raw = env.get(ENV_ROLES, "").strip()
        if not department and not roles_raw:
            return cls.demo()
        if not department or not roles_raw:
            raise PrincipalConfigError(f"{ENV_DEPARTMENT} 与 {ENV_ROLES} 必须同时设置")
        roles = tuple(dict.fromkeys(r.strip() for r in roles_raw.split(",") if r.strip()))
        try:
            return cls(user_id=env.get(ENV_USER_ID, "").strip() or f"mcp-{department}",
                       department=department, roles=roles)
        except ValidationError as exc:
            raise PrincipalConfigError(f"可信身份配置无效: {exc.errors()[0]['msg']}") from exc
