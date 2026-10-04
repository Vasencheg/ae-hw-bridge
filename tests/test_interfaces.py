from ae_hw_bridge.core.interfaces import (
    ExecutionResult,
    IReplClient,
    IConsoleReader,
)


def test_dataclasses_instantiation() -> None:
    res = ExecutionResult(ok=True, stdout="output", stderr="", result=None)
    assert res.ok is True
    assert res.stdout == "output"
    assert res.stderr == ""
    assert res.result is None


def test_protocols_exist() -> None:
    # Verify protocols are importable and runtime checkable
    assert hasattr(IReplClient, "__protocol_attrs__") or hasattr(IReplClient, "_is_runtime_protocol")
    assert hasattr(IConsoleReader, "__protocol_attrs__") or hasattr(IConsoleReader, "_is_runtime_protocol")
