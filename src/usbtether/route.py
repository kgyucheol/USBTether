"""Wi-Fi 없이도 동작하게 만드는 대체 경로.

방화벽이 트래픽을 가로채려면 커널이 먼저 그 패킷을 보낼 경로를 찾아야 한다.
Wi-Fi 를 끄면 기본 경로가 사라져 앱의 connect() 가 "네트워크 도달 불가"로
즉시 실패하고, 가로챌 패킷 자체가 생기지 않는다.

데이터는 USB 로 나가는데도 Wi-Fi 가 필요해지는 셈이라, 더미 인터페이스에
아주 낮은 우선순위의 기본 경로를 깔아 둔다. 실제 회선이 있으면 그쪽이 쓰이고,
없으면 이 경로가 패킷을 받아 방화벽까지 흘려보낸다.

DNS 서버 주소도 Wi-Fi 와 함께 사라지므로 같이 붙여 준다.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("usbtether.route")

LINK = "usbt0"
V4_ADDR = "10.255.255.1/30"
V6_ADDR = "fd00:u5b:7e7::1/64"
# 실제 회선보다 훨씬 큰 값. 진짜 경로가 있으면 그쪽이 이긴다.
METRIC = "30000"


def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(args), capture_output=True, text=True)
    if check and proc.returncode != 0:
        log.debug("%s 실패: %s", " ".join(args), proc.stderr.strip())
    return proc


def _ip() -> str | None:
    return shutil.which("ip") or ("/usr/sbin/ip" if shutil.which("/usr/sbin/ip") else None)


def exists() -> bool:
    ip = _ip()
    if not ip:
        return False
    return _run(ip, "link", "show", LINK).returncode == 0


def install(dns_servers: list[str] | None = None) -> bool:
    """대체 경로를 만든다. 이미 있으면 그대로 둔다."""
    ip = _ip()
    if not ip:
        log.warning("ip 명령이 없어 대체 경로를 만들 수 없습니다")
        return False

    if not exists():
        proc = _run(ip, "link", "add", LINK, "type", "dummy")
        if proc.returncode != 0:
            log.warning("더미 인터페이스 생성 실패: %s", proc.stderr.strip())
            return False

    _run(ip, "link", "set", LINK, "up")
    _run(ip, "addr", "add", V4_ADDR, "dev", LINK)
    _run(ip, "-6", "addr", "add", V6_ADDR, "dev", LINK)
    _run(ip, "route", "add", "default", "dev", LINK, "metric", METRIC)
    _run(ip, "-6", "route", "add", "default", "dev", LINK, "metric", METRIC)

    _attach_dns(dns_servers or [])
    log.info("대체 경로 %s 준비됨 (Wi-Fi 없이도 동작)", LINK)
    return True


def _attach_dns(servers: list[str]) -> None:
    """Wi-Fi 가 꺼지면 DNS 서버 주소도 사라진다. 더미 링크에 붙여 둔다.

    어차피 방화벽이 DNS 를 폰으로 돌리므로 여기 적는 주소는 '어디로 물어볼지'를
    정하는 표식일 뿐이다.
    """
    resolvectl = shutil.which("resolvectl")
    if not resolvectl or not servers:
        return
    if _run(resolvectl, "dns", LINK, *servers).returncode == 0:
        # '~.' 는 모든 도메인을 이 링크로 물어보라는 뜻
        _run(resolvectl, "domain", LINK, "~.")
        log.info("대체 DNS 등록: %s", ", ".join(servers))


def remove() -> None:
    """더미 인터페이스를 지운다. 딸린 주소·경로·DNS 설정도 함께 사라진다."""
    ip = _ip()
    if not ip or not exists():
        return
    _run(ip, "link", "del", LINK)
    log.info("대체 경로 %s 제거됨", LINK)
