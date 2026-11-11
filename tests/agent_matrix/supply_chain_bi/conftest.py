import pytest
from agent_matrix.supply_chain_bi.agent import SupplyChainBIAgent
from agent_matrix.supply_chain_bi.sandbox_sqlite import SqliteReadOnlySandbox
from agent_matrix.supply_chain_bi.models import SupplyChainAgentConfig
from .sim_db import seed_database
from .fakes import ScriptedSqlModel, StaticScopeResolver, SpySandbox

@pytest.fixture
async def db_path(tmp_path):
    return await seed_database(tmp_path/'sc_sim.db')

@pytest.fixture
async def sandbox(db_path):
    return SqliteReadOnlySandbox(db_path)

@pytest.fixture
def make_agent(sandbox):
    def make(*,model=None,resolver=None,spy=None,**config):
        return SupplyChainBIAgent(sql_model=model or ScriptedSqlModel(),sandbox=spy or SpySandbox(sandbox),scope_resolver=resolver or StaticScopeResolver(),config=SupplyChainAgentConfig(**config))
    return make
