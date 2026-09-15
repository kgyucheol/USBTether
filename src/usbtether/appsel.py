"""앱별 라우팅.

선택한 앱만 폰 회선을 쓰게 하려면 그 앱들을 전용 cgroup에 모아야 한다.
방화벽은 "이 cgroup에서 나온 트래픽만" 폰으로 보낸다.

  user.slice/user-<uid>.slice/user@<uid>.service/usbtether.slice
      ├── usbtether-holder.service     slice 를 살아있게 유지 (nft 규칙이 경로를 요구)
      ├── run-*.scope                   여기서 새로 실행한 앱
      └── adopted/                      이미 떠 있던 프로세스를 옮겨온 곳
"""

from __future__ import annotations

import os
import shutil
import subprocess

SLICE = "usbtether.slice"
HOLDER_UNIT = "usbtether-holder.service"
CGROUP_ROOT = "/sys/fs/cgroup"
ADOPTED = "adopted"


def cgroup_rel_path(uid: int | None = None) -> str:
    """nftables 매칭에 쓰는 cgroup v2 상대 경로."""
    uid = os.getuid() if uid is None else uid
    return f"user.slice/user-{uid}.slice/user@{uid}.service/{SLICE}"


def slice_fs_path(uid: int | None = None) -> str:
    return os.path.join(CGROUP_ROOT, cgroup_rel_path(uid))


def slice_exists(uid: int | None = None) -> bool:
    return os.path.isdir(slice_fs_path(uid))


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def ensure_slice() -> str:
    """slice 를 만들고 살아있게 유지한다. cgroup 상대 경로를 돌려준다."""
    if not slice_exists():
        subprocess.run(
            ["systemd-run", "--user", "--quiet",
             f"--unit={HOLDER_UNIT}", f"--slice={SLICE}",
             "--property=Type=simple", "--", "sleep", "infinity"],
            capture_output=True, text=True,
        )
    leaf = os.path.join(slice_fs_path(), ADOPTED)
    try:
        os.makedirs(leaf, exist_ok=True)
    except OSError:
        pass
    return cgroup_rel_path()


def release_slice() -> None:
    _systemctl("stop", HOLDER_UNIT)


def launch(exec_target: str, desktop_file: str | None = None) -> tuple[bool, str]:
    """앱을 폰 회선 cgroup 안에서 새로 실행한다."""
    if desktop_file and shutil.which("gio"):
        cmd = ["gio", "launch", desktop_file]
    else:
        cmd = ["sh", "-c", exec_target]
    proc = subprocess.run(
        ["systemd-run", "--user", "--quiet", "--scope", f"--slice={SLICE}",
         "--collect", "--", *cmd],
        capture_output=True, text=True, start_new_session=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "실행 실패"
    return True, ""


def adopt_pid(pid: int) -> bool:
    """이미 실행 중인 프로세스를 폰 회선 cgroup으로 옮긴다."""
    leaf = os.path.join(slice_fs_path(), ADOPTED, "cgroup.procs")
    try:
        with open(leaf, "w", encoding="ascii") as fh:
            fh.write(str(pid))
        return True
    except OSError:
        return False


def adopt_tree(pid: int) -> int:
    """프로세스와 그 자식들을 함께 옮긴다. 옮긴 개수를 돌려준다."""
    moved = 0
    for target in [pid, *_descendants(pid)]:
        if adopt_pid(target):
            moved += 1
    return moved


def _descendants(pid: int) -> list[int]:
    out: list[int] = []
    stack = [pid]
    while stack:
        current = stack.pop()
        try:
            with open(f"/proc/{current}/task/{current}/children", encoding="ascii") as fh:
                kids = [int(x) for x in fh.read().split()]
        except (OSError, ValueError):
            continue
        out.extend(kids)
        stack.extend(kids)
    return out


def routed_pids() -> list[int]:
    """현재 폰 회선으로 나가고 있는 프로세스 PID 목록."""
    base = slice_fs_path()
    found: list[int] = []
    for root, _dirs, files in os.walk(base):
        if "cgroup.procs" not in files:
            continue
        try:
            with open(os.path.join(root, "cgroup.procs"), encoding="ascii") as fh:
                found.extend(int(line) for line in fh if line.strip())
        except (OSError, ValueError):
            continue
    return sorted(set(found))


def process_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def list_desktop_apps() -> list[dict]:
    """설치된 GUI 앱 목록. GUI에서 선택 목록을 그릴 때 쓴다."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except (ImportError, ValueError):
        return []

    apps = []
    for info in Gio.AppInfo.get_all():
        if not info.should_show():
            continue
        apps.append({
            "id": info.get_id() or "",
            "name": info.get_display_name() or info.get_name() or "",
            "exec": info.get_commandline() or "",
            "icon": info.get_icon().to_string() if info.get_icon() else "",
            "desktop_file": getattr(info, "get_filename", lambda: None)() or "",
        })
    apps.sort(key=lambda a: a["name"].lower())
    return apps
