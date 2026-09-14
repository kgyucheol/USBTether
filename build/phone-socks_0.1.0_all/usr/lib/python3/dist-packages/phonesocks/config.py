"""설정 로드/저장. 데몬은 시스템 설정, GUI는 사용자 설정을 쓴다."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field

SYSTEM_CONFIG = "/etc/phone-socks/config.json"
RUN_DIR = "/run/phone-socks"
STATE_FILE = os.path.join(RUN_DIR, "state.json")

# 내부 리스너 포트. 폰의 SOCKS 포트와 겹치지 않게 고른다.
TPROXY_PORT = 12345
DNS_PORT = 15353

# 프록시에서 제외할 대역 — 로컬/사설망은 원래 회선으로 그대로 나간다.
BYPASS_V4 = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "224.0.0.0/4", "240.0.0.0/4",
]
BYPASS_V6 = ["::1/128", "fc00::/7", "fe80::/10", "ff00::/8"]


@dataclass
class Config:
    # 폰 쪽 SOCKS5 앱이 listen 중인 포트
    phone_socks_port: int = 1080
    # 노트북에서 쓸 로컬 포트
    local_socks_port: int = 1080
    # 특정 기기만 쓸 때의 adb serial ("" 이면 자동 선택)
    device_serial: str = ""

    # "all"    : 노트북 전체 트래픽을 폰으로
    # "apps"   : 선택한 앱만 폰으로
    mode: str = "all"

    # mode="apps" 일 때 폰 회선을 쓸 앱들의 .desktop 파일 ID
    selected_apps: list[str] = field(default_factory=list)

    # DNS 질의를 보낼 상위 서버 (폰을 통해 TCP로 전달된다)
    dns_upstream: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])

    # UDP는 폰 SOCKS가 중계하지 못한다. 막지 않으면 원래 회선으로 새어나간다.
    block_udp_leak: bool = True
    # ping 등 ICMP도 중계 불가. 막으면 완전 차단, 풀면 원래 회선으로 나간다.
    block_icmp_leak: bool = False
    # USB 재연결 시 자동 복구
    auto_reconnect: bool = True

    @classmethod
    def load(cls, path: str = SYSTEM_CONFIG) -> "Config":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str = SYSTEM_CONFIG) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def as_dict(self) -> dict:
        return asdict(self)
