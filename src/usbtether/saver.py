"""데이터 절약 — 켜져 있는 동안 자동 업데이트를 보류한다.

노트북이 모바일 데이터로 나가는 동안 배포판이 알아서 업데이트를 받으면
사용자가 모르는 사이에 수백 MB 가 빠져나간다.

원칙 세 가지:

1. 목록을 미리 정해 두지 않는다. 시스템에 실제로 설치된 타이머를 훑어
   업데이트성 작업을 찾아낸다. 사용자마다 깔린 것이 다르기 때문이다.
2. 무엇을 보류할지는 사용자가 고른다. 찾아낸 것을 그대로 보여주고 끄고 켤 수 있다.
3. 사람이 직접 시작한 일은 절대 막지 않는다. `apt upgrade`, `snap refresh`,
   앱스토어의 '업데이트' 버튼은 그대로 동작한다. 보류하는 것은 '예약된 자동
   실행'뿐이다. 모바일 회선으로 업데이트를 받고 싶은 사람도 있기 때문이다.

되돌릴 때는 실제로 바꾼 것만 되돌린다.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("usbtether.saver")

AUTO = "auto"          # "찾아낸 것 전부"를 뜻하는 설정값
SNAP_ID = "snap:auto-refresh"

# 예약 실행으로 네트워크에서 무언가를 받아오는 작업들
UPDATER_HINTS = (
    "apt-daily", "unattended-upgrade", "packagekit", "fwupd",
    "snap", "flatpak", "dnf-", "yum-", "rpm-ostree",
    "update-notifier", "motd-news", "ubuntu-advantage", "ua-", "esm-",
)
# 이름은 비슷해도 네트워크를 쓰지 않는 것들
NOT_UPDATERS = (
    "logrotate", "man-db", "fstrim", "e2scrub", "tmpfiles",
    "systemd-journal", "anacron", "plocate", "mlocate",
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run("systemctl", *args)


def _snap() -> str | None:
    return shutil.which("snap")


def _timer_active(unit: str) -> bool:
    return _systemctl("is-active", "--quiet", unit).returncode == 0


def _installed_timers() -> list[tuple[str, str]]:
    """(유닛 이름, 설명) 목록. 시스템에 실제로 깔려 있는 타이머만."""
    proc = _systemctl("list-units", "--type=timer", "--all",
                      "--no-pager", "--no-legend", "--plain")
    found = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].endswith(".timer"):
            continue
        found.append((parts[0], parts[4].strip()))
    return found


def _looks_like_updater(unit: str, description: str) -> bool:
    haystack = f"{unit} {description}".lower()
    if any(word in haystack for word in NOT_UPDATERS):
        return False
    return any(word in unit.lower() for word in UPDATER_HINTS)


def _snap_held() -> bool:
    snap = _snap()
    if not snap:
        return False
    proc = _run(snap, "refresh", "--time")
    return "hold:" in proc.stdout.lower()


def discover() -> list[dict]:
    """이 시스템에서 예약 실행되는 업데이트 작업들을 찾는다."""
    items = []
    for unit, description in _installed_timers():
        if not _looks_like_updater(unit, description):
            continue
        items.append({
            "id": unit,
            "kind": "timer",
            "name": description or unit,
            "detail": unit,
            "running": _timer_active(unit),
        })

    if _snap() is not None:
        items.append({
            "id": SNAP_ID,
            "kind": "snap",
            "name": "snap 자동 갱신",
            "detail": "snapd 가 주기적으로 스스로 확인합니다",
            "running": not _snap_held(),
        })

    items.sort(key=lambda item: item["name"])
    return items


def _selected(choices: list[str], items: list[dict]) -> list[dict]:
    if not choices or AUTO in choices:
        return items
    return [item for item in items if item["id"] in choices]


def engage(choices: list[str] | None = None) -> dict:
    """고른 자동 업데이트를 보류한다. 실제로 바꾼 것만 기록해 돌려준다."""
    items = discover()
    targets = _selected(choices or [AUTO], items)

    stopped, snap_held = [], False
    for item in targets:
        if not item["running"]:
            continue  # 원래 안 돌던 건 건드리지 않는다
        if item["kind"] == "timer":
            if _systemctl("stop", item["id"]).returncode == 0:
                stopped.append(item["id"])
        elif item["kind"] == "snap":
            snap = _snap()
            if not snap:
                continue
            # 자동 갱신만 보류된다. 사람이 직접 실행하는 snap refresh 는 그대로 동작한다.
            proc = _run(snap, "refresh", "--hold")
            if proc.returncode == 0:
                snap_held = True
            else:
                log.warning("snap 갱신 보류 실패: %s",
                            (proc.stderr or proc.stdout).strip()
                            or f"종료 코드 {proc.returncode}")

    state = {"timers": stopped, "snap_held": snap_held}
    if stopped or snap_held:
        log.info("자동 업데이트 보류 — 타이머 %d개%s",
                 len(stopped), ", snap 갱신" if snap_held else "")
    return state


def release(state: dict | None) -> None:
    """engage() 가 바꾼 것만 정확히 되돌린다."""
    if not state:
        return
    for unit in state.get("timers", []):
        _systemctl("start", unit)
    if state.get("snap_held"):
        snap = _snap()
        if snap:
            _run(snap, "refresh", "--unhold")
    if state.get("timers") or state.get("snap_held"):
        log.info("자동 업데이트 원복")


def release_everything() -> None:
    """비상시. 무엇을 멈췄는지 모르는 상태에서 되살린다."""
    for item in discover():
        if item["kind"] == "timer":
            _systemctl("start", item["id"])
    snap = _snap()
    if snap and _snap_held():
        _run(snap, "refresh", "--unhold")
