from mobyai.context.contracts import extract_python_contract, render_contract


SOURCE = '''\
from dataclasses import dataclass
from pydantic import BaseModel

SECRET = 1

def connect(dsn: str) -> int:
    return 1

def _hidden() -> None:
    return None

@dataclass
class Row:
    id: int
    name: str

class Store(BaseModel):
    path: str

    def get(self, key: str) -> str:
        return key

class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    def ping(self) -> str:
        return "ok"

    def _internal(self) -> None:
        return None
'''


def test_extract_public_functions_and_schemas() -> None:
    payload = extract_python_contract("src/db.py", SOURCE)
    assert payload is not None
    names = [item["name"] for item in payload["functions"]]
    assert names == ["connect"]
    assert payload["functions"][0]["args"] == ["dsn: str"]
    assert payload["functions"][0]["returns"] == "int"
    by_class = {item["name"]: item for item in payload["classes"]}
    assert set(by_class) == {"Row", "Store", "Service"}
    assert by_class["Row"]["schema"] is True
    assert "id: int" in by_class["Row"]["fields"]
    assert by_class["Store"]["schema"] is True
    assert by_class["Store"]["methods"][0]["name"] == "get"
    assert "store: Store" in by_class["Service"]["init_args"]
    assert all(item["name"] != "_internal" for item in by_class["Service"]["methods"])
    rendered = render_contract("src/db.py", payload)
    assert rendered.startswith("src/db.py")
    assert "def connect(dsn: str) -> int" in rendered
    assert "_hidden" not in rendered
    assert "class Row" in rendered


def test_syntax_error_returns_none() -> None:
    assert extract_python_contract("src/bad.py", "def broken(") is None


def test_empty_module_renders_placeholder() -> None:
    payload = extract_python_contract("src/empty.py", "x = 1\n")
    assert payload is not None
    assert render_contract("src/empty.py", payload) == "src/empty.py\n(no public surface)"


def test_signatures_preserve_calling_conventions() -> None:
    payload = extract_python_contract("api.py", """
async def fetch(key: str, /, retries: int = 3, *, timeout=10):
    pass
class API:
    @staticmethod
    def parse(value, *, strict=True):
        pass
    @classmethod
    def create(cls, value=None):
        pass
""")
    rendered = render_contract("api.py", payload)
    assert "async def fetch(key: str, /, retries: int = 3, *, timeout = 10)" in rendered
    assert "@staticmethod" in rendered
    assert "def parse(value, *, strict = True)" in rendered
    assert "def create(value = None)" in rendered


async def test_stale_contract_is_not_used(tmp_path) -> None:
    from datetime import datetime, UTC
    from mobyai.context.contracts import source_digest
    from mobyai.context.packer import ContextPacker
    from mobyai.context.types import FileRole
    from mobyai.execution.filesystem import WorkspaceFilesystem
    from mobyai.execution.workspace import Workspace
    from mobyai.models.contract import ModuleContract
    from mobyai.engine.planning import _module_contract_listings
    from mobyai.state.memory import InMemoryStateStore
    workspace = Workspace(tmp_path)
    files = WorkspaceFilesystem(workspace)
    await files.write_file("api.py", "def new_api(): pass")
    contract = ModuleContract(project_id="p", task_id="t", path="api.py",
        rendered="def old_api(): pass", payload={"source_sha256": source_digest("old")},
        updated_at=datetime.now(UTC))
    packed = await ContextPacker(files)._read_packed("api.py", FileRole.IMPORT, {"api.py": contract})
    assert not packed.from_contract
    assert "new_api" in packed.content
    store = InMemoryStateStore()
    await store.contracts.upsert(contract)
    assert await _module_contract_listings(store, "p", workspace) == []
    contract.payload["source_sha256"] = source_digest("def new_api(): pass")
    await store.contracts.upsert(contract)
    assert await _module_contract_listings(store, "p", workspace, token_budget=1) == []
    (tmp_path / "api.py").unlink()
    assert await _module_contract_listings(store, "p", workspace) == []
