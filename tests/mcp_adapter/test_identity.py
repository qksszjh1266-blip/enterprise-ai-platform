"""可信身份 CallerPrincipal 单元测试。"""

import pytest
from pydantic import ValidationError

from mcp_server.identity import (
    DEMO_DEPARTMENT, DEMO_ROLES, ENV_DEPARTMENT, ENV_ROLES, ENV_USER_ID, CallerPrincipal, PrincipalConfigError,
)


def test_defaults_to_lowest_privilege_demo_identity():
    principal = CallerPrincipal.from_env({})
    assert principal.department == DEMO_DEPARTMENT
    assert principal.roles == DEMO_ROLES


def test_reads_env_and_dedupes_roles():
    principal = CallerPrincipal.from_env(
        {ENV_USER_ID: "u-1", ENV_DEPARTMENT: "finance", ENV_ROLES: " finance , finance_manager,finance "})
    assert principal.user_id == "u-1"
    assert principal.department == "finance"
    assert principal.roles == ("finance", "finance_manager")


def test_user_id_falls_back_to_department_tag():
    assert CallerPrincipal.from_env({ENV_DEPARTMENT: "store_ops", ENV_ROLES: "clerk"}).user_id == "mcp-store_ops"


@pytest.mark.parametrize("env", [{ENV_DEPARTMENT: "store_ops"}, {ENV_ROLES: "clerk"}])
def test_partial_identity_config_is_rejected(env):
    with pytest.raises(PrincipalConfigError):
        CallerPrincipal.from_env(env)


@pytest.mark.parametrize("env", [
    {ENV_DEPARTMENT: "Store Ops", ENV_ROLES: "clerk"},
    {ENV_DEPARTMENT: "store_ops", ENV_ROLES: "clerk,ADMIN*"},
    {ENV_DEPARTMENT: "store_ops", ENV_ROLES: " , "},
])
def test_malformed_identity_config_is_rejected(env):
    with pytest.raises(PrincipalConfigError):
        CallerPrincipal.from_env(env)


def test_permits_only_own_department_and_roles():
    principal = CallerPrincipal(user_id="u", department="store_ops", roles=("clerk", "store_manager"))
    assert principal.permits(department="store_ops", role="clerk")
    assert principal.permits(department="store_ops", role="store_manager")
    assert not principal.permits(department="finance", role="clerk")
    assert not principal.permits(department="store_ops", role="finance")


def test_principal_is_immutable():
    principal = CallerPrincipal.demo()
    with pytest.raises(ValidationError):
        principal.department = "finance"
