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
