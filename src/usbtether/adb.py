"""adb 로 폰과의 USB 연결을 다룬다."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger("usbtether.adb")


class AdbError(Exception):
    pass


def _adb(*args: str, serial: str = "", timeout: float = 15.0) -> subprocess.CompletedProcess:
    exe = shutil.which("adb")
    if not exe:
        raise AdbError("adb 명령이 없습니다. android-tools-adb 패키지를 설치하세요.")
    cmd = [exe]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb 응답 없음: {' '.join(args)}") from exc


def available() -> bool:
    return shutil.which("adb") is not None


def devices() -> list[dict]:
    """연결된 기기 목록. state 가 'device' 여야 사용 가능하다."""
    proc = _adb("devices", "-l")
    found = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        match = re.search(r"model:(\S+)", line)
        if match:
            model = match.group(1).replace("_", " ")
        found.append({"serial": serial, "state": state, "model": model})
    return found


STATE_HINTS = {
    "unauthorized": "폰 화면에 뜬 'USB 디버깅을 허용하시겠습니까?'에서 허용을 눌러주세요",
    "offline": "폰이 응답하지 않습니다. USB 케이블을 다시 꽂아보세요",
    "no permissions": "USB 접근 권한이 없습니다. 케이블을 다시 꽂거나 adb 를 재시작하세요",
}


def connection_problem() -> str:
    """기기는 보이는데 쓸 수 없는 상태라면 그 이유를 사람 말로 돌려준다."""
    found = devices()
    if not found:
        return "USB 로 연결된 폰이 없습니다. 케이블과 USB 디버깅 설정을 확인하세요"
    if any(d["state"] == "device" for d in found):
        return ""
    for dev in found:
        for key, hint in STATE_HINTS.items():
            if key in dev["state"]:
                return hint
    return f"폰이 사용할 수 없는 상태입니다: {found[0]['state']}"


def ready_device(preferred: str = "") -> dict | None:
    usable = [d for d in devices() if d["state"] == "device"]
    if not usable:
        return None
    if preferred:
        for dev in usable:
            if dev["serial"] == preferred:
                return dev
        return None
    return usable[0]


def forward(local_port: int, phone_port: int, serial: str = "") -> bool:
    _adb("forward", "--remove", f"tcp:{local_port}", serial=serial)
    proc = _adb("forward", f"tcp:{local_port}", f"tcp:{phone_port}", serial=serial)
    return proc.returncode == 0


def forward_active(local_port: int, phone_port: int, serial: str = "") -> bool:
    proc = _adb("forward", "--list", serial=serial)
    return f"tcp:{local_port} tcp:{phone_port}" in proc.stdout


def remove_forward(local_port: int, serial: str = "") -> None:
    _adb("forward", "--remove", f"tcp:{local_port}", serial=serial)


def phone_port_listening(port: int, serial: str = "") -> bool:
    """폰에서 해당 포트가 실제로 열려 있는지."""
    proc = _adb("shell", "netstat -tln", serial=serial)
    return bool(re.search(rf"[:.]{port}\s", proc.stdout))


def phone_listen_ports(serial: str = "") -> list[int]:
    proc = _adb("shell", "netstat -tln", serial=serial)
    ports = set()
    for line in proc.stdout.splitlines():
        if "LISTEN" not in line:
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        tail = fields[3].rsplit(":", 1)[-1]
        if tail.isdigit():
            ports.add(int(tail))
    return sorted(ports)


def transport(serial: str = "") -> str:
    """폰이 어떤 회선으로 나가는지: CELLULAR / WIFI / UNKNOWN."""
    proc = _adb("shell", "dumpsys connectivity", serial=serial, timeout=20)
    match = re.search(r"Active default network:\s*(\d+)", proc.stdout)
    if not match:
        return "UNKNOWN"
    net_id = match.group(1)
    for line in proc.stdout.splitlines():
        if f"network{{{net_id}}}" in line:
            found = re.search(r"Transports:\s*([A-Z_|]+)", line)
            if found:
                return found.group(1)
    return "UNKNOWN"
