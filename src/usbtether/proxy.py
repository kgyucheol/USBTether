"""투명 프록시 엔진.

방화벽이 리다이렉트한 TCP 연결을 받아 원래 목적지를 알아낸 뒤 폰 SOCKS5로 중계하고,
UDP DNS 질의는 TCP로 바꿔 폰을 통해 내보낸다.
(폰 앱이 UDP ASSOCIATE를 지원하지 않기 때문에 DNS만 따로 처리한다.)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct

from . import firewall, socks5

log = logging.getLogger("usbtether.proxy")

SO_ORIGINAL_DST = 80          # netfilter가 원래 목적지를 보관하는 소켓 옵션
IP6T_SO_ORIGINAL_DST = 80
BUF = 65536


def original_destination(sock: socket.socket) -> tuple[str, int]:
    """REDIRECT 되기 전의 진짜 목적지를 커널에서 꺼낸다."""
    is_v6 = sock.family == socket.AF_INET6
    if is_v6:
        try:
            raw = sock.getsockopt(socket.IPPROTO_IPV6, IP6T_SO_ORIGINAL_DST, 28)
            port, addr = struct.unpack(">H", raw[2:4])[0], raw[8:24]
            host = socket.inet_ntop(socket.AF_INET6, addr)
            # ::ffff:1.2.3.4 형태는 IPv4로 되돌린다
            if host.startswith("::ffff:"):
                host = host[7:]
            return host, port
        except OSError:
            pass  # v4-mapped 소켓이면 아래 IPv4 경로로 떨어진다
    raw = sock.getsockopt(socket.IPPROTO_IP, SO_ORIGINAL_DST, 16)
    port = struct.unpack(">H", raw[2:4])[0]
    host = socket.inet_ntoa(raw[4:8])
    return host, port


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    moved = 0
    try:
        while True:
            chunk = await reader.read(BUF)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            moved += len(chunk)
    except (ConnectionResetError, BrokenPipeError, TimeoutError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass
    return moved


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, RuntimeError):
        pass


class Stats:
    def __init__(self) -> None:
        self.active = 0
        self.total = 0
        self.failed = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.dns_queries = 0
        self.dns_failed = 0
        self.direct = 0
        self.via_phone = 0

    def snapshot(self) -> dict:
        return {
            "active": self.active, "total": self.total, "failed": self.failed,
            "bytes_up": self.bytes_up, "bytes_down": self.bytes_down,
            "dns_queries": self.dns_queries, "dns_failed": self.dns_failed,
            "direct": self.direct, "via_phone": self.via_phone,
        }


async def open_direct(host: str, port: int, timeout: float):
    """원래 회선으로 직접 연결한다.

    소켓에 표식(mark)을 찍는다. 이게 없으면 방화벽이 이 연결마저 다시
    프록시로 돌려보내 자기 자신에게 무한히 되돌아온다.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, firewall.DIRECT_MARK)
    except (OSError, AttributeError) as exc:
        sock.close()
        raise OSError("소켓 표식을 찍을 수 없습니다 (CAP_NET_ADMIN 필요)") from exc

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout)
    except BaseException:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock)


class TransparentTCPProxy:
    """리다이렉트된 TCP 연결을 처리한다.

    direct_first 가 꺼져 있으면 무조건 폰으로 보낸다(전체 모드).
    켜져 있으면 원래 회선을 먼저 시도하고, 안 될 때만 폰으로 넘긴다.
    판정 결과는 방화벽 집합에 넣어 두므로 다음부터는 커널이 알아서 처리한다.
    """

    def __init__(self, listen_port: int, socks_host: str, socks_port: int, stats: Stats,
                 direct_first: bool = False, direct_timeout: float = 4.0,
                 on_verdict=None):
        self.listen_port = listen_port
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.stats = stats
        self.direct_first = direct_first
        self.direct_timeout = direct_timeout
        self.on_verdict = on_verdict
        # 막힌 것으로 판정된 주소. 여기 있으면 직결을 다시 시도하지 않는다.
        self.blocked: set[str] = set()
        self._servers: list[asyncio.AbstractServer] = []
        # 종료할 때 끊어야 할 연결들. 이걸 안 잡아두면 wait_closed() 가
        # 열려 있는 연결이 전부 끝날 때까지 기다려 서비스 정지가 멎는다.
        self._open: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        for host in ("127.0.0.1", "::1"):
            try:
                server = await asyncio.start_server(
                    self._handle, host, self.listen_port, reuse_address=True
                )
                self._servers.append(server)
            except OSError as exc:
                log.warning("TCP 리스너 실패 %s:%s — %s", host, self.listen_port, exc)
        if not self._servers:
            raise RuntimeError(f"{self.listen_port} 포트를 열 수 없습니다")
        log.info("투명 TCP 프록시 시작 (포트 %s)", self.listen_port)

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
        # 살아 있는 연결을 먼저 끊는다. 그래야 wait_closed() 가 즉시 돌아온다.
        for writer in list(self._open):
            try:
                writer.transport.abort()
            except (AttributeError, OSError, RuntimeError):
                pass
        self._open.clear()
        for server in self._servers:
            try:
                await asyncio.wait_for(server.wait_closed(), 3)
            except Exception:
                pass
        self._servers.clear()

    async def _upstream(self, host: str, port: int):
        """이 목적지로 나갈 길을 고른다."""
        if not self.direct_first:
            return await socks5.open_connection(
                self.socks_host, self.socks_port, host, port
            )
        if host in self.blocked:
            # 이미 막힌 것으로 아는 곳. 직결을 다시 시도하지 않는다.
            self.stats.via_phone += 1
            return await socks5.open_connection(
                self.socks_host, self.socks_port, host, port
            )

        try:
            pair = await open_direct(host, port, self.direct_timeout)
        except (OSError, TimeoutError) as exc:
            log.debug("직결 실패 %s:%s (%s) — 폰으로 재시도", host, port, exc)
        else:
            self.stats.direct += 1
            self._verdict(host, reachable=True)
            return pair

        pair = await socks5.open_connection(
            self.socks_host, self.socks_port, host, port
        )
        self.stats.via_phone += 1
        self.blocked.add(host)
        self._verdict(host, reachable=False)
        log.info("원래 회선으로 막힌 곳 → 폰 경유: %s:%s", host, port)
        return pair

    def _verdict(self, host: str, reachable: bool) -> None:
        if self.on_verdict is None:
            return
        try:
            self.on_verdict(host, reachable)
        except Exception as exc:   # 판정 기록이 실패해도 연결은 살려야 한다
            log.debug("판정 기록 실패 %s: %s", host, exc)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            host, port = original_destination(sock)
        except OSError as exc:
            log.debug("원래 목적지 조회 실패: %s", exc)
            await _close(writer)
            return

        self.stats.total += 1
        self.stats.active += 1
        try:
            up_reader, up_writer = await self._upstream(host, port)
        except (socks5.Socks5Error, OSError, asyncio.IncompleteReadError, TimeoutError) as exc:
            self.stats.failed += 1
            self.stats.active -= 1
            log.debug("중계 실패 %s:%s — %s", host, port, exc)
            await _close(writer)
            return

        self._open.add(writer)
        self._open.add(up_writer)
        try:
            up, down = await asyncio.gather(
                _pump(reader, up_writer), _pump(up_reader, writer)
            )
            self.stats.bytes_up += up
            self.stats.bytes_down += down
        finally:
            self.stats.active -= 1
            self._open.discard(writer)
            self._open.discard(up_writer)
            await _close(up_writer)
            await _close(writer)


class DNSOverTCPForwarder(asyncio.DatagramProtocol):
    """UDP DNS 질의를 받아 TCP DNS로 바꿔 폰을 통해 전달한다."""

    def __init__(self, socks_host: str, socks_port: int, upstreams: list[str], stats: Stats):
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.upstreams = upstreams or ["1.1.1.1"]
        self.stats = stats
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 12:
            return
        self.stats.dns_queries += 1
        asyncio.get_running_loop().create_task(self._resolve(data, addr))

    async def _resolve(self, query: bytes, addr) -> None:
        for server in self.upstreams:
            try:
                answer = await self._ask(server, query)
            except (socks5.Socks5Error, OSError, asyncio.IncompleteReadError, TimeoutError):
                continue
            if answer and self.transport is not None:
                self.transport.sendto(answer, addr)
                return
        self.stats.dns_failed += 1
        if self.transport is not None:
            # SERVFAIL 로 응답해 클라이언트가 무한정 기다리지 않게 한다
            self.transport.sendto(query[:2] + b"\x81\x82" + query[4:12], addr)

    async def _ask(self, server: str, query: bytes, timeout: float = 6.0) -> bytes:
        reader, writer = await socks5.open_connection(
            self.socks_host, self.socks_port, server, 53, timeout=timeout
        )
        try:
            writer.write(struct.pack(">H", len(query)) + query)
            await writer.drain()
            head = await asyncio.wait_for(reader.readexactly(2), timeout)
            size = struct.unpack(">H", head)[0]
            return await asyncio.wait_for(reader.readexactly(size), timeout)
        finally:
            await _close(writer)


class DNSProxy:
    def __init__(self, listen_port: int, socks_host: str, socks_port: int,
                 upstreams: list[str], stats: Stats):
        self.listen_port = listen_port
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.upstreams = upstreams
        self.stats = stats
        self._transports: list[asyncio.DatagramTransport] = []
        self._tcp_servers: list[asyncio.AbstractServer] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for host in ("127.0.0.1", "::1"):
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: DNSOverTCPForwarder(
                        self.socks_host, self.socks_port, self.upstreams, self.stats
                    ),
                    local_addr=(host, self.listen_port),
                    reuse_port=False,
                )
                self._transports.append(transport)
            except OSError as exc:
                log.warning("DNS 리스너 실패 %s:%s — %s", host, self.listen_port, exc)

        # TCP 53 으로 오는 질의도 같은 포트로 리다이렉트되므로 함께 받는다
        for host in ("127.0.0.1", "::1"):
            try:
                server = await asyncio.start_server(
                    self._handle_tcp, host, self.listen_port, reuse_address=True
                )
                self._tcp_servers.append(server)
            except OSError:
                pass

        if not self._transports:
            raise RuntimeError(f"DNS 포트 {self.listen_port} 를 열 수 없습니다")
        log.info("DNS over TCP 포워더 시작 (포트 %s → %s)", self.listen_port, self.upstreams)

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readexactly(2), 6)
            query = await asyncio.wait_for(reader.readexactly(struct.unpack(">H", head)[0]), 6)
        except (asyncio.IncompleteReadError, TimeoutError, OSError):
            await _close(writer)
            return

        self.stats.dns_queries += 1
        fwd = DNSOverTCPForwarder(self.socks_host, self.socks_port, self.upstreams, self.stats)
        for server in fwd.upstreams:
            try:
                answer = await fwd._ask(server, query)
            except (socks5.Socks5Error, OSError, asyncio.IncompleteReadError, TimeoutError):
                continue
            writer.write(struct.pack(">H", len(answer)) + answer)
            await writer.drain()
            break
        else:
            self.stats.dns_failed += 1
        await _close(writer)

    async def stop(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        for server in self._tcp_servers:
            server.close()
        for server in self._tcp_servers:
            try:
                await asyncio.wait_for(server.wait_closed(), 3)
            except Exception:
                pass
        self._tcp_servers.clear()
