"""명령줄 인터페이스."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import __version__, appsel, client

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty() else ("",) * 6
)


def _public_ip(proxy: str | None = None, timeout: int = 10) -> str:
    cmd = ["curl", "-s", "--max-time", str(timeout)]
    cmd += ["-x", proxy] if proxy else ["--noproxy", "*"]
    cmd.append("https://ifconfig.me")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return proc.stdout.strip() or "(응답 없음)"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(확인 실패)"


def cmd_status(args) -> int:
    data = client.status()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    on = data["enabled"]
    print()
    print(f"  {BOLD}phone-socks{RESET}")
    print(f"  상태        : {GREEN + 'ON' + RESET if on else RED + 'OFF' + RESET}")
    if on:
        mode_label = "전체 트래픽" if data["mode"] == "all" else "선택한 앱만"
        print(f"  모드        : {mode_label}")
        minutes, seconds = divmod(data["uptime"], 60)
        print(f"  가동        : {minutes}분 {seconds}초")

    device = data.get("device")
    if device:
        print(f"  폰          : {device['model'] or device['serial']}"
              + (f" ({data['transport']})" if data.get("transport") else ""))
    else:
        print(f"  폰          : {YELLOW}연결 안 됨{RESET}")

    print(f"  방화벽 규칙 : {'적용됨' if data['firewall_active'] else '없음'}")
    print(f"  폰 SOCKS    : {'응답함' if data['socks_ok'] else RED + '무응답' + RESET}")

    stats = data["stats"]
    if on:
        up_mb = stats["bytes_up"] / 1048576
        down_mb = stats["bytes_down"] / 1048576
        print(f"  연결        : 활성 {stats['active']} / 누적 {stats['total']}"
              f" / 실패 {stats['failed']}")
        print(f"  전송량      : ↑ {up_mb:.1f} MB  ↓ {down_mb:.1f} MB")
        print(f"  DNS         : {stats['dns_queries']}건 (실패 {stats['dns_failed']})")

    if data.get("last_error"):
        print(f"  {YELLOW}경고        : {data['last_error']}{RESET}")
    print()
    return 0


def cmd_on(args) -> int:
    mode = "apps" if args.apps else "all"
    cgroup = appsel.ensure_slice() if mode == "apps" else None
    client.enable(mode=mode, cgroup_path=cgroup)
    print(f"{GREEN}✔{RESET} 켜짐 — {'선택한 앱만' if mode == 'apps' else '노트북 전체 트래픽이'} 폰 회선을 사용합니다")
    if mode == "apps":
        print(f"  앱 실행: phone-socks run <명령>")
    print(f"  공인 IP: {_public_ip()}")
    return 0


def cmd_off(args) -> int:
    client.disable()
    appsel.release_slice()
    print(f"{GREEN}✔{RESET} 꺼짐 — 원래 회선으로 복귀했습니다")
    print(f"  공인 IP: {_public_ip()}")
    return 0


def cmd_toggle(args) -> int:
    return cmd_off(args) if client.status()["enabled"] else cmd_on(args)


def cmd_test(args) -> int:
    data = client.status()
    print()
    print(f"  현재 공인 IP : {_public_ip()}")
    print(f"  상태         : {'폰 회선 경유 중' if data['enabled'] else '원래 회선'}")
    print()
    return 0


def cmd_run(args) -> int:
    """선택한 앱만 모드에서, 이 명령을 폰 회선으로 실행한다."""
    appsel.ensure_slice()
    ok, err = appsel.launch(" ".join(args.command))
    if not ok:
        print(f"{RED}✘{RESET} 실행 실패: {err}", file=sys.stderr)
        return 1
    print(f"{GREEN}✔{RESET} 폰 회선으로 실행했습니다: {' '.join(args.command)}")
    return 0


def cmd_adopt(args) -> int:
    appsel.ensure_slice()
    moved = appsel.adopt_tree(args.pid)
    if moved:
        print(f"{GREEN}✔{RESET} 프로세스 {moved}개를 폰 회선으로 옮겼습니다")
        return 0
    print(f"{RED}✘{RESET} 옮기지 못했습니다 (PID 확인)", file=sys.stderr)
    return 1


def cmd_apps(args) -> int:
    pids = appsel.routed_pids()
    if not pids:
        print("폰 회선으로 나가는 프로세스가 없습니다")
        return 0
    print("\n  폰 회선 사용 중인 프로세스")
    for pid in pids:
        print(f"    {pid:>7}  {appsel.process_name(pid)}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone-socks",
        description="폰의 모바일 데이터를 노트북 인터넷 회선으로 사용합니다.",
    )
    parser.add_argument("--version", action="version", version=f"phone-socks {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_on = sub.add_parser("on", help="켜기")
    p_on.add_argument("--apps", action="store_true", help="전체 대신 선택한 앱만 폰 회선으로")
    p_on.set_defaults(func=cmd_on)

    sub.add_parser("off", help="끄기").set_defaults(func=cmd_off)
    sub.add_parser("toggle", help="전환").set_defaults(func=cmd_toggle)

    p_status = sub.add_parser("status", help="상태 보기")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    sub.add_parser("test", help="현재 공인 IP 확인").set_defaults(func=cmd_test)

    p_run = sub.add_parser("run", help="이 명령만 폰 회선으로 실행")
    p_run.add_argument("command", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run)

    p_adopt = sub.add_parser("adopt", help="실행 중인 프로세스를 폰 회선으로 옮기기")
    p_adopt.add_argument("pid", type=int)
    p_adopt.set_defaults(func=cmd_adopt)

    sub.add_parser("apps", help="폰 회선 사용 중인 프로세스 보기").set_defaults(func=cmd_apps)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["status"])
    try:
        return args.func(args)
    except client.DaemonError as exc:
        print(f"{RED}✘{RESET} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
