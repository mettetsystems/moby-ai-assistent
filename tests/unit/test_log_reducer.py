from mobyai.context.log_reducer import LogReducer
from mobyai.execution.commands import CommandResult


def test_short_log_stays_in_head() -> None:
    reduced = LogReducer(tail_lines=150).reduce(
        CommandResult(
            command=["pytest", "tests/test_db.py"],
            exit_code=0,
            stdout="ok\n",
            stderr="",
            duration_seconds=0.2,
        )
    )
    assert reduced.stdout_head.strip() == "ok"
    assert reduced.stdout_tail == ""
    assert reduced.failed_tests == []


def test_long_log_is_split_and_errors_extracted() -> None:
    stdout_lines = [f"line-{index}" for index in range(1, 21)]
    stderr = """\
Traceback (most recent call last):
  File "tests/test_db.py", line 4, in test_connect
    assert got == want
AssertionError
E       AssertionError: assert 1 == 2
FAILED tests/test_db.py::test_connect
"""
    reduced = LogReducer(tail_lines=5).reduce(
        CommandResult(
            command=["pytest", "tests/test_db.py"],
            exit_code=1,
            stdout="\n".join(stdout_lines) + "\n",
            stderr=stderr,
            duration_seconds=1.0,
        )
    )
    assert reduced.stdout_head.startswith("line-1")
    assert reduced.stdout_tail.endswith("line-20")
    assert "line-6" not in reduced.stdout_head
    assert reduced.failed_tests == ["tests/test_db.py::test_connect"]
    assert reduced.traceback is not None
    assert "AssertionError" in reduced.traceback
    assert any("assert 1 == 2" in item for item in reduced.assertion_differences)
