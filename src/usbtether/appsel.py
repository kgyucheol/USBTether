"""앱 단위 라우팅.

특정 앱만 다른 길로 보내려면 그 앱들을 전용 cgroup 에 모아야 한다.
방화벽은 "이 cgroup 에서 나온 트래픽"을 골라 처리한다. 두 가지 용도가 있다.

  usbtether.slice          '선택한 앱만' 모드 — 여기 든 앱만 폰으로
  usbtether_direct.slice   '전체' 모드의 예외 — 여기 든 앱만 원래 회선으로

slice 이름에 '-' 를 쓰면 systemd 가 계층으로 해석한다(usbtether-x.slice 는
usbtether.slice 의 자식이 된다). 두 slice 가 섞이지 않도록 '_' 를 쓴다.

  user.slice/user-<uid>.slice/user@<uid>.service/<slice>
      ├── <slice 이름>-holder.service   slice 를 살아있게 유지 (nft 규칙이 경로를 요구)
      ├── run-*.scope                    여기서 새로 실행한 앱
      └── adopted/                       이미 떠 있던 프로세스를 옮겨온 곳

snap 앱은 실행되자마자 자기 cgroup(snap.<앱>.*.scope)으로 스스로 옮겨가므로
'여기서 실행하면 적용' 방식으로는 붙잡을 수 없다. 그래서 이름으로 지정한 앱을
주기적으로 찾아 옮겨 두는 방식(pin)을 함께 둔다.
"""

from __future__ import annotations

import os
import shutil
import subprocess

SLICE = "usbtether.slice"
DIRECT_SLICE = "usbtether_direct.slice"
CGROUP_ROOT = "/sys/fs/cgroup"
ADOPTED = "adopted"

# 이름이 겹쳐도 옮기면 안 되는 것들 — 데스크톱이 망가진다
NEVER_PIN = {
    "systemd", "gnome-shell", "gnome-session-binary", "Xwayland", "dbus-daemon",
    "pipewire", "wireplumber", "pulseaudio", "bash", "sh", "zsh", "sudo",
    "python3", "usbtether", "usbtether-tray", "usbtether-gui",
}


# 데스크톱 구성요소. 앱 후보 목록에서 뺀다.
SYSTEM_PREFIXES = (
    "gnome-", "gsd-", "evolution-", "xdg-", "at-spi", "dconf", "gcr-",
    "update-notifier", "user-session", "ibus", "tracker-", "goa-",
)


def _holder(slice_name: str) -> str:
    return slice_name.removesuffix(".slice") + "-holder.service"


def cgroup_rel_path(uid: int | None = None, slice_name: str = SLICE) -> str:
    """nftables 매칭에 쓰는 cgroup v2 상대 경로."""
    uid = os.getuid() if uid is None else uid
    return f"user.slice/user-{uid}.slice/user@{uid}.service/{slice_name}"


def slice_fs_path(uid: int | None = None, slice_name: str = SLICE) -> str:
    return os.path.join(CGROUP_ROOT, cgroup_rel_path(uid, slice_name))


def slice_exists(uid: int | None = None, slice_name: str = SLICE) -> bool:
    return os.path.isdir(slice_fs_path(uid, slice_name))


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def ensure_slice(slice_name: str = SLICE) -> str:
    """slice 를 만들고 살아있게 유지한다. cgroup 상대 경로를 돌려준다."""
    if not slice_exists(slice_name=slice_name):
        subprocess.run(
            ["systemd-run", "--user", "--quiet",
             f"--unit={_holder(slice_name)}", f"--slice={slice_name}",
             "--property=Type=simple", "--", "sleep", "infinity"],
            capture_output=True, text=True,
        )
    leaf = os.path.join(slice_fs_path(slice_name=slice_name), ADOPTED)
    try:
        os.makedirs(leaf, exist_ok=True)
    except OSError:
        pass
    return cgroup_rel_path(slice_name=slice_name)


def release_slice(slice_name: str = SLICE) -> None:
    _systemctl("stop", _holder(slice_name))


def launch(exec_target: str, desktop_file: str | None = None,
           slice_name: str = SLICE) -> tuple[bool, str]:
    """앱을 지정한 cgroup 안에서 새로 실행한다. (snap 앱은 빠져나가므로 pin 을 쓴다)"""
    if desktop_file and shutil.which("gio"):
        cmd = ["gio", "launch", desktop_file]
    else:
        cmd = ["sh", "-c", exec_target]
    proc = subprocess.run(
        ["systemd-run", "--user", "--quiet", "--scope", f"--slice={slice_name}",
         "--collect", "--", *cmd],
        capture_output=True, text=True, start_new_session=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "실행 실패"
    return True, ""


def adopt_pid(pid: int, slice_name: str = SLICE) -> bool:
    """이미 실행 중인 프로세스를 지정한 cgroup 으로 옮긴다."""
    leaf = os.path.join(slice_fs_path(slice_name=slice_name), ADOPTED, "cgroup.procs")
    try:
        with open(leaf, "w", encoding="ascii") as fh:
            fh.write(str(pid))
        return True
    except OSError:
        return False


def adopt_tree(pid: int, slice_name: str = SLICE) -> int:
    """프로세스와 그 자식들을 함께 옮긴다. 옮긴 개수를 돌려준다."""
    moved = 0
    for target in [pid, *_descendants(pid)]:
        if adopt_pid(target, slice_name):
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


def routed_pids(slice_name: str = SLICE) -> list[int]:
    """지정한 cgroup 안에 있는 프로세스 PID 목록."""
    base = slice_fs_path(slice_name=slice_name)
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


def program_name(pid: int) -> str:
    """사람이 알아볼 프로그램 이름. 실행 파일 이름을 우선하고, 없으면 comm.

    comm 은 15자로 잘리고 스레드 이름으로 바뀌기도 해서 실행 파일 쪽이 믿을 만하다.
    (예: 크롬은 comm 도 실행 파일도 'chrome', snap 파이어폭스는 실행 파일이 'firefox')
    """
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        name = os.path.basename(exe).removesuffix(" (deleted)")
        if name:
            return name
    except OSError:
        pass
    return process_name(pid)


def _cgroup_of(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as fh:
            return fh.read().strip().rsplit(":", 1)[-1]
    except OSError:
        return ""


def _my_pids() -> list[int]:
    uid = os.getuid()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid == uid:
                pids.append(int(entry))
        except OSError:
            continue
    return pids


def pin(names: list[str], slice_name: str = DIRECT_SLICE) -> int:
    """이름이 일치하는 실행 중 프로세스를 지정한 cgroup 으로 옮긴다.

    이미 들어가 있는 것은 건드리지 않는다. 주기적으로 불러 쓰면, 앱을 어떻게
    실행하든(독, 터미널, 재시작) 늘 그 cgroup 에 머물게 된다.
    옮긴 프로세스 수를 돌려준다.
    """
    wanted = {n.strip() for n in names if n.strip()} - NEVER_PIN
    if not wanted:
        return 0
    marker = "/" + slice_name + "/"
    me = os.getpid()
    moved = 0
    for pid in _my_pids():
        if pid == me or program_name(pid) not in wanted:
            continue
        if marker in _cgroup_of(pid) + "/":
            continue
        moved += adopt_tree(pid, slice_name)
    return moved


def prepare_enable(mode: str) -> tuple[str | None, str | None]:
    """켜기 직전에 사용자 쪽에서 준비할 것. (cgroup_path, exclude_cgroup) 을 돌려준다.

    cgroup 은 사용자 소유라 데몬(root, 파일 권한 무시 불가)이 만들 수 없다.
    '전체' 모드에서는 예외 slice 를 늘 만들어 둔다. 비어 있어도 해가 없고,
    나중에 예외 앱을 추가할 때 다시 켤 필요가 없어진다.
    """
    if mode == "apps":
        return ensure_slice(SLICE), None
    if mode == "all":
        return None, ensure_slice(DIRECT_SLICE)
    return None, None


def running_programs() -> list[dict]:
    """지금 떠 있는 데스크톱 앱들. 예외로 고를 후보를 보여줄 때 쓴다.

    데스크톱에서 실행한 앱은 app.slice 아래(snap 이면 snap.* 스코프)에 있다.
    """
    # 실행 단위(scope)마다 가장 먼저 뜬 프로세스를 그 앱의 대표로 본다.
    # 같은 scope 안의 보조 프로세스(crashpad, 렌더러 등)는 후보에서 뺀다.
    leaders: dict[str, int] = {}
    members: dict[str, int] = {}
    for pid in _my_pids():
        group = _cgroup_of(pid)
        if "/app.slice/" not in group and f"/{DIRECT_SLICE}/" not in group:
            continue
        unit = group.rsplit("/", 1)[-1]
        # 백그라운드 서비스(.service)와 터미널에서 띄운 명령(vte-spawn-*)은 앱이 아니다
        if not unit.endswith(".scope") or unit.startswith("vte-spawn"):
            if f"/{DIRECT_SLICE}/" not in group:
                continue
        members[group] = members.get(group, 0) + 1
        if group not in leaders or pid < leaders[group]:
            leaders[group] = pid

    counts: dict[str, int] = {}
    for group, pid in leaders.items():
        name = program_name(pid)
        if not name or name in NEVER_PIN or name.startswith(SYSTEM_PREFIXES):
            continue
        counts[name] = counts.get(name, 0) + members[group]
    return [{"name": n, "processes": c} for n, c in sorted(counts.items())]


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
