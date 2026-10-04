"""IPC protocol definitions and line-delimited JSON serialization for AE-HW-BRIDGE daemon."""

import json
from dataclasses import dataclass, field
from typing import Any, Optional
import uuid

import hashlib
from pathlib import Path
from ae_hw_bridge import __version__

DEFAULT_SOCKET_PATH = "/tmp/ae_hw_bridge.sock"
DEFAULT_LOCK_PATH = "/tmp/ae_hw_bridge_daemon.lock"
DEFAULT_SPAWN_LOCK_PATH = "/tmp/ae_hw_bridge_spawn.lock"


def get_daemon_signature() -> str:
    """Compute combined signature from package version and daemon source code hash.

    Ensures client can detect if a running daemon was started on an older revision
    and automatically restart it.
    """
    h = hashlib.sha256()
    h.update(__version__.encode("utf-8"))
    daemon_dir = Path(__file__).resolve().parent
    core_dir = daemon_dir.parent / "core"

    for src_dir in (daemon_dir, core_dir):
        if src_dir.exists() and src_dir.is_dir():
            for py_file in sorted(src_dir.glob("*.py")):
                try:
                    h.update(py_file.read_bytes())
                except OSError:
                    pass
    return f"{__version__}-{h.hexdigest()[:10]}"


@dataclass
class Request:
    """IPC Request from client to daemon."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "method": self.method, "params": self.params})


@dataclass
class Response:
    """IPC Response from daemon to client."""

    id: str
    ok: bool
    result: Any = None
    error: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "ok": self.ok, "result": self.result, "error": self.error})


def encode_request(req: Request) -> bytes:
    """Encode request as newline-terminated UTF-8 JSON bytes."""
    return f"{req.to_json()}\n".encode("utf-8")


def decode_request(line: str) -> Request:
    """Decode raw JSON string line into Request."""
    data = json.loads(line)
    return Request(
        id=str(data.get("id", "")),
        method=str(data.get("method", "")),
        params=data.get("params", {}),
    )


def encode_response(resp: Response) -> bytes:
    """Encode response as newline-terminated UTF-8 JSON bytes."""
    return f"{resp.to_json()}\n".encode("utf-8")


def decode_response(line: str) -> Response:
    """Decode raw JSON string line into Response."""
    data = json.loads(line)
    return Response(
        id=str(data.get("id", "")),
        ok=bool(data.get("ok", False)),
        result=data.get("result"),
        error=data.get("error"),
    )
