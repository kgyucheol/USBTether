"""nftables 규칙 관리.

전용 테이블 하나만 만들고 끌 때 통째로 지운다. 사용자의 기존 방화벽 규칙은 건드리지 않는다.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from .config import BYPASS_V4, BYPASS_V6, DNS_PORT, TPROXY_PORT

log = logging.getLogger("usbtether.firewall")

TABLE = "usbtether"


class FirewallError(Exception):
    pass


def _nft(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    exe = shutil.which("nft")
    if not exe:
        raise FirewallError("nft 명령이 없습니다. nftables 패키지를 설치하세요.")
    proc = subprocess.run([exe, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise FirewallError(f"nft {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc


def cgroup_match(cgroup_path: str) -> str:
    """cgroup v2 경로를 nftables 매칭 구문으로 바꾼다."""
    clean = cgroup_path.strip("/")
    level = len(clean.split("/"))
    return f'socket cgroupv2 level {level} "{clean}"'


def build_ruleset(
    mode: str = "all",
    cgroup_path: str | None = None,
    tproxy_port: int = TPROXY_PORT,
    dns_port: int = DNS_PORT,
    block_udp: bool = True,
    block_icmp: bool = False,
) -> str:
    """적용할 nftables 규칙 전문을 만든다.

    규칙 순서가 중요하다. DNS 리다이렉트는 사설망 예외보다 **먼저** 와야 한다.
    공유기 DNS(예: 192.168.x.1)는 사설망 주소라서, 예외를 먼저 두면
    도메인 조회가 폰이 아니라 원래 회선으로 새어나간다.
    """
    if mode == "apps":
        if not cgroup_path:
            raise FirewallError("앱 선택 모드인데 cgroup 경로가 없습니다")
        # 선택된 cgroup 에서 나온 트래픽만 처리하고, 나머지는 손대지 않는다
        nat_entry = f'        {cgroup_match(cgroup_path)} jump route_phone'
        filter_entry = f'        {cgroup_match(cgroup_path)} jump leak_guard'
    else:
        nat_entry = "        jump route_phone"
        filter_entry = "        jump leak_guard"

    v4 = ", ".join(BYPASS_V4)
    v6 = ", ".join(BYPASS_V6)

    leak_rules = []
    if block_udp:
        leak_rules.append("        udp dport { 67, 68 } return")
        leak_rules.append("        meta l4proto udp drop")
    if block_icmp:
        leak_rules.append("        meta l4proto { icmp, ipv6-icmp } drop")
    leak_body = "\n".join(leak_rules) if leak_rules else "        return"

    return f"""table inet {TABLE} {{
    set bypass4 {{
        type ipv4_addr
        flags interval
        elements = {{ {v4} }}
    }}

    set bypass6 {{
        type ipv6_addr
        flags interval
        elements = {{ {v6} }}
    }}

    chain route_phone {{
        # DNS 먼저. 목적지가 사설망이어도 폰으로 보낸다.
        meta l4proto tcp th dport 53 redirect to :{dns_port}
        meta l4proto udp th dport 53 redirect to :{dns_port}
        # 로컬/사설망은 원래대로 둔다
        ip daddr @bypass4 return
        ip6 daddr @bypass6 return
        meta l4proto tcp redirect to :{tproxy_port}
    }}

    chain leak_guard {{
        # DNS 는 위에서 이미 폰으로 돌려놨다
        udp dport 53 return
        tcp dport 53 return
        ip daddr @bypass4 return
        ip6 daddr @bypass6 return
{leak_body}
    }}

    chain output_nat {{
        type nat hook output priority -100; policy accept;
        oif "lo" return
{nat_entry}
    }}

    chain output_filter {{
        type filter hook output priority 0; policy accept;
        oif "lo" return
{filter_entry}
    }}
}}
"""


def is_active() -> bool:
    proc = _nft("list", "table", "inet", TABLE, check=False)
    return proc.returncode == 0


def apply(ruleset: str) -> None:
    clear()
    proc = subprocess.run(
        [shutil.which("nft") or "nft", "-f", "-"],
        input=ruleset, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FirewallError(f"규칙 적용 실패: {proc.stderr.strip()}")
    log.info("nftables 규칙 적용됨 (table inet %s)", TABLE)


def clear() -> None:
    if is_active():
        _nft("delete", "table", "inet", TABLE)
        log.info("nftables 규칙 제거됨")


def check(ruleset: str) -> tuple[bool, str]:
    """실제 적용 없이 문법만 검사한다."""
    proc = subprocess.run(
        [shutil.which("nft") or "nft", "-c", "-f", "-"],
        input=ruleset, capture_output=True, text=True,
    )
    return proc.returncode == 0, proc.stderr.strip()


def flush_conntrack() -> None:
    """이미 맺어진 연결이 옛 경로를 계속 쓰지 않도록 추적 정보를 비운다."""
    exe = shutil.which("conntrack")
    if exe:
        subprocess.run([exe, "-F"], capture_output=True, text=True)
