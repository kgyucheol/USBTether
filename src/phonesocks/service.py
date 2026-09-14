"""특권 데몬.

하는 일: adb forward 유지, 투명 프록시 구동, nftables 규칙 적용, 제어 소켓 응대.
GUI/CLI 는 이 데몬에 유닉스 소켓으로 명령하고, 권한은 polkit 이 판정한다.

안전장치: 어떤 경로로 죽든 nftables 규칙은 반드시 지워진다.
규칙만 남으면 사용자의 인터넷이 통째로 끊기기 때문이다.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import time

from . import adb, firewall, socks5
from .config import Config, DNS_PORT, RUN_DIR, STATE_FILE, TPROXY_PORT
from .proxy import DNSProxy, Stats, TransparentTCPProxy

log = logging.getLogger("phonesocks.service")

SOCKET_PATH = os.path.join(RUN_DIR, "control.sock")
POLKIT_ACTION = "org.phonesocks.manage"


class ManagerError(Exception):
    pass


class Manager:
    def __init__(self, config: Config):
        self.config = config
        self.stats = Stats()
        self.enabled = False
        self.mode = "all"
        self.cgroup_path: str | None = None
        self.started_at = 0.0
        self.last_error = ""
        self._tcp: TransparentTCPProxy | None = None
        self._dns: DNSProxy | None = None
        self._watchdog: asyncio.Task | None = None
        self._transport = ""
        self._transport_at = 0.0
        self._transport_serial = ""

    # ------------------------------------------------------------ 켜기/끄기
    async def enable(self, mode: str = "all", cgroup_path: str | None = None) -> None:
        if self.enabled:
            raise ManagerError("이미 켜져 있습니다")

        device = await asyncio.to_thread(adb.ready_device, self.config.device_serial)
        if device is None:
            raise ManagerError("USB 디버깅된 폰을 찾지 못했습니다")

        serial = device["serial"]
        listening = await asyncio.to_thread(
            adb.phone_port_listening, self.config.phone_socks_port, serial
        )
        if not listening:
            ports = await asyncio.to_thread(adb.phone_listen_ports, serial)
            raise ManagerError(
                f"폰의 {self.config.phone_socks_port}번 포트에 SOCKS 서버가 없습니다. "
                f"열려 있는 포트: {ports}"
            )

        forwarded = await asyncio.to_thread(
            adb.forward, self.config.local_socks_port, self.config.phone_socks_port, serial
        )
        if not forwarded:
            raise ManagerError("adb forward 실패")

        if not await asyncio.to_thread(
            socks5.probe_sync, "127.0.0.1", self.config.local_socks_port
        ):
            adb.remove_forward(self.config.local_socks_port, serial)
            raise ManagerError("폰 SOCKS5 서버가 응답하지 않습니다")

        ruleset = firewall.build_ruleset(
            mode=mode,
            cgroup_path=cgroup_path,
            tproxy_port=TPROXY_PORT,
            dns_port=DNS_PORT,
            block_udp=self.config.block_udp_leak,
            block_icmp=self.config.block_icmp_leak,
        )

        self._tcp = TransparentTCPProxy(
            TPROXY_PORT, "127.0.0.1", self.config.local_socks_port, self.stats
        )
        self._dns = DNSProxy(
            DNS_PORT, "127.0.0.1", self.config.local_socks_port,
            self.config.dns_upstream, self.stats,
        )
        # 프록시를 먼저 띄운 뒤에 방화벽을 건다. 순서가 바뀌면 그 사이 트래픽이 죽는다.
        await self._tcp.start()
        await self._dns.start()

        try:
            firewall.apply(ruleset)
        except firewall.FirewallError:
            await self._tcp.stop()
            await self._dns.stop()
            adb.remove_forward(self.config.local_socks_port, serial)
            raise

        firewall.flush_conntrack()
        self.enabled = True
        self.mode = mode
        self.cgroup_path = cgroup_path
        self.started_at = time.time()
        self.last_error = ""
        self.config.device_serial = serial
        self._watchdog = asyncio.create_task(self._watch())
        self._write_state()
        log.info("켜짐 (모드=%s, 기기=%s)", mode, serial)

    async def disable(self) -> None:
        # 방화벽부터 내린다. 프록시가 먼저 죽으면 트래픽이 갈 곳을 잃는다.
        try:
            firewall.clear()
        except firewall.FirewallError as exc:
            log.error("방화벽 정리 실패: %s", exc)

        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if self._tcp:
            await self._tcp.stop()
            self._tcp = None
        if self._dns:
            await self._dns.stop()
            self._dns = None

        if adb.available():
            adb.remove_forward(self.config.local_socks_port, self.config.device_serial)

        firewall.flush_conntrack()
        self.enabled = False
        self.cgroup_path = None
        self._write_state()
        log.info("꺼짐")

    # ------------------------------------------------------------ 감시
    async def _watch(self) -> None:
        """USB 가 잠깐 빠지거나 폰 앱이 재시작되면 forward 를 다시 세운다."""
        while self.enabled:
            try:
                await asyncio.sleep(5)
                if not self.config.auto_reconnect:
                    continue
                serial = self.config.device_serial
                if adb.ready_device(serial) is None:
                    self.last_error = "폰 연결 끊김"
                    continue
                if not adb.forward_active(
                    self.config.local_socks_port, self.config.phone_socks_port, serial
                ):
                    if adb.forward(
                        self.config.local_socks_port, self.config.phone_socks_port, serial
                    ):
                        log.info("adb forward 복구됨")
                        self.last_error = ""
                elif not socks5.probe_sync("127.0.0.1", self.config.local_socks_port, 3):
                    self.last_error = "폰 SOCKS 서버 무응답"
                else:
                    self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 감시가 죽으면 복구가 멈추므로 넓게 잡는다
                log.warning("감시 오류: %s", exc)

    # ------------------------------------------------------------ 상태
    def _cached_transport(self, serial: str) -> str:
        """dumpsys 는 느리다. 상태 조회마다 부르지 않도록 30초 캐시를 둔다."""
        now = time.time()
        if self._transport_at + 30 < now or self._transport_serial != serial:
            self._transport = adb.transport(serial)
            self._transport_at = now
            self._transport_serial = serial
        return self._transport

    def status(self) -> dict:
        device = adb.ready_device(self.config.device_serial) if adb.available() else None
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "cgroup_path": self.cgroup_path,
            "uptime": int(time.time() - self.started_at) if self.enabled else 0,
            "device": device,
            "transport": self._cached_transport(device["serial"]) if device else "",
            "firewall_active": firewall.is_active(),
            "socks_ok": socks5.probe_sync("127.0.0.1", self.config.local_socks_port, 2),
            "last_error": self.last_error,
            "stats": self.stats.snapshot(),
            "config": self.config.as_dict(),
        }

    def _write_state(self) -> None:
        os.makedirs(RUN_DIR, exist_ok=True)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"enabled": self.enabled, "mode": self.mode}, fh)
        except OSError:
            pass


# ---------------------------------------------------------------- 권한 판정
def _peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)  # pid, uid, gid


def _process_start_time(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return 0


async def authorized(sock: socket.socket) -> tuple[bool, str]:
    """polkit 에 이 호출자가 프록시를 조작해도 되는지 물어본다.

    사용자가 인증 창에 비밀번호를 넣는 동안 이벤트 루프가 멈추면 안 되므로
    별도 스레드에서 기다린다.
    """
    pid, uid, _gid = _peer_credentials(sock)
    if uid == 0:
        return True, ""
    start = _process_start_time(pid)
    proc = await asyncio.to_thread(
        subprocess.run,
        ["pkcheck", "--action-id", POLKIT_ACTION,
         "--process", f"{pid},{start},{uid}", "--allow-user-interaction"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, ""
    return False, "권한이 거부되었습니다"


# ---------------------------------------------------------------- 제어 소켓
class ControlServer:
    READ_ONLY = {"status", "ping", "apps_probe"}

    def __init__(self, manager: Manager):
        self.manager = manager
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        os.makedirs(RUN_DIR, exist_ok=True)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self._server = await asyncio.start_unix_server(self._handle, SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)  # 접근 허가는 polkit 이 판정한다
        log.info("제어 소켓 %s", SOCKET_PATH)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            if not line:
                return
            request = json.loads(line)
            response = await self._dispatch(request, writer.get_extra_info("socket"))
        except (json.JSONDecodeError, TimeoutError) as exc:
            response = {"ok": False, "error": f"잘못된 요청: {exc}"}
        except Exception as exc:
            log.exception("요청 처리 실패")
            response = {"ok": False, "error": str(exc)}

        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except OSError:
            pass
        writer.close()

    async def _dispatch(self, request: dict, sock: socket.socket) -> dict:
        cmd = request.get("cmd", "")
        args = request.get("args", {}) or {}

        if cmd not in self.READ_ONLY:
            allowed, reason = await authorized(sock)
            if not allowed:
                return {"ok": False, "error": reason}

        try:
            if cmd == "ping":
                return {"ok": True, "data": {"pong": True}}
            if cmd == "status":
                return {"ok": True, "data": self.manager.status()}
            if cmd == "enable":
                await self.manager.enable(
                    mode=args.get("mode", "all"), cgroup_path=args.get("cgroup_path")
                )
                return {"ok": True, "data": self.manager.status()}
            if cmd == "disable":
                await self.manager.disable()
                return {"ok": True, "data": self.manager.status()}
            if cmd == "set_config":
                for key, value in (args.get("config") or {}).items():
                    if hasattr(self.manager.config, key):
                        setattr(self.manager.config, key, value)
                self.manager.config.save()
                return {"ok": True, "data": self.manager.config.as_dict()}
            return {"ok": False, "error": f"알 수 없는 명령: {cmd}"}
        except (ManagerError, firewall.FirewallError, adb.AdbError) as exc:
            return {"ok": False, "error": str(exc)}

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass


# ---------------------------------------------------------------- 진입점
def _emergency_cleanup() -> None:
    """어떤 경로로 죽든 방화벽 규칙은 남기지 않는다."""
    try:
        firewall.clear()
    except Exception:
        subprocess.run(["nft", "delete", "table", "inet", firewall.TABLE],
                       capture_output=True)


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.load()
    manager = Manager(config)
    control = ControlServer(manager)
    await control.start()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    log.info("phone-socks 데몬 준비됨")
    await stopping.wait()
    log.info("종료 신호 수신")
    if manager.enabled:
        await manager.disable()
    await control.stop()


CAP_NET_ADMIN_BIT = 12


def has_net_admin() -> bool:
    """root 여부가 아니라 실제로 방화벽을 다룰 능력이 있는지 본다."""
    if os.geteuid() == 0:
        return True
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << CAP_NET_ADMIN_BIT))
    except (OSError, ValueError):
        pass
    return False


def main() -> int:
    atexit.register(_emergency_cleanup)
    if not has_net_admin():
        print("이 데몬은 CAP_NET_ADMIN 권한이 필요합니다 "
              "(systemctl start phone-socks 로 실행하세요).", flush=True)
        return 1
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        _emergency_cleanup()
    return 0
