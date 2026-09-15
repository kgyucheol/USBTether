"""데몬과 대화하는 클라이언트. CLI와 GUI가 같이 쓴다."""

from __future__ import annotations

import json
import socket

from .service import SOCKET_PATH


class DaemonError(Exception):
    pass


def call(cmd: str, timeout: float = 60.0, **args) -> dict:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
    except FileNotFoundError:
        raise DaemonError(
            "데몬이 실행 중이 아닙니다.  sudo systemctl start usbtether"
        ) from None
    except OSError as exc:
        raise DaemonError(f"데몬에 연결할 수 없습니다: {exc}") from None

    with sock:
        payload = json.dumps({"cmd": cmd, "args": args}, ensure_ascii=False) + "\n"
        sock.sendall(payload.encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if data.endswith(b"\n"):
                break

    if not chunks:
        raise DaemonError("데몬이 응답하지 않았습니다")
    response = json.loads(b"".join(chunks).decode())
    if not response.get("ok"):
        raise DaemonError(response.get("error", "알 수 없는 오류"))
    return response.get("data", {})


def running() -> bool:
    try:
        call("ping", timeout=5)
        return True
    except DaemonError:
        return False


def status() -> dict:
    return call("status", timeout=30)


def updaters() -> dict:
    return call("updaters", timeout=40)


def enable(mode: str = "all", cgroup_path: str | None = None) -> dict:
    return call("enable", mode=mode, cgroup_path=cgroup_path, timeout=90)


def disable() -> dict:
    return call("disable", timeout=60)


def set_config(**values) -> dict:
    return call("set_config", config=values)
