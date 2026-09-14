"""폰의 SOCKS5 서버와 대화하는 최소 클라이언트."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct

VER = 0x05
NO_AUTH = 0x00
CMD_CONNECT = 0x01
ATYP_IPV4, ATYP_DOMAIN, ATYP_IPV6 = 0x01, 0x03, 0x04

REPLY_TEXT = {
    0x00: "성공",
    0x01: "일반 SOCKS 서버 오류",
    0x02: "규칙상 거부",
    0x03: "네트워크 도달 불가",
    0x04: "호스트 도달 불가",
    0x05: "연결 거부됨",
    0x06: "TTL 만료",
    0x07: "명령 미지원",
    0x08: "주소 형식 미지원",
}


class Socks5Error(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"SOCKS5 오류 {code}: {REPLY_TEXT.get(code, '알 수 없음')}")


def _addr_payload(host: str, port: int) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raw = host.encode("idna")
        if len(raw) > 255:
            raise ValueError("호스트 이름이 너무 깁니다")
        return bytes([ATYP_DOMAIN, len(raw)]) + raw + struct.pack(">H", port)
    if ip.version == 4:
        return bytes([ATYP_IPV4]) + ip.packed + struct.pack(">H", port)
    return bytes([ATYP_IPV6]) + ip.packed + struct.pack(">H", port)


async def _read_bound_addr(reader: asyncio.StreamReader) -> None:
    """CONNECT 응답 뒤에 붙는 BND.ADDR/PORT를 읽어 버린다."""
    atyp = (await reader.readexactly(1))[0]
    if atyp == ATYP_IPV4:
        await reader.readexactly(4)
    elif atyp == ATYP_IPV6:
        await reader.readexactly(16)
    elif atyp == ATYP_DOMAIN:
        n = (await reader.readexactly(1))[0]
        await reader.readexactly(n)
    else:
        raise Socks5Error(0x08)
    await reader.readexactly(2)


async def open_connection(
    proxy_host: str,
    proxy_port: int,
    dest_host: str,
    dest_port: int,
    timeout: float = 15.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """프록시를 거쳐 dest_host:dest_port 로 연결된 스트림을 돌려준다."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port), timeout
    )
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        writer.write(bytes([VER, 1, NO_AUTH]))
        await writer.drain()
        ver, method = await asyncio.wait_for(reader.readexactly(2), timeout)
        if ver != VER or method != NO_AUTH:
            raise Socks5Error(0x01)

        writer.write(bytes([VER, CMD_CONNECT, 0x00]) + _addr_payload(dest_host, dest_port))
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(3), timeout)
        if head[1] != 0x00:
            raise Socks5Error(head[1])
        await _read_bound_addr(reader)
        return reader, writer
    except Exception:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        raise


def probe_sync(proxy_host: str, proxy_port: int, timeout: float = 5.0) -> bool:
    """동기 방식 헬스체크 — SOCKS5 핸드셰이크만 해 본다."""
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=timeout) as s:
            s.sendall(bytes([VER, 1, NO_AUTH]))
            return s.recv(2) == bytes([VER, NO_AUTH])
    except OSError:
        return False


def supports_udp(proxy_host: str, proxy_port: int, timeout: float = 5.0) -> bool:
    """UDP ASSOCIATE 지원 여부. 대부분의 폰 앱은 지원하지 않는다."""
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=timeout) as s:
            s.sendall(bytes([VER, 1, NO_AUTH]))
            if s.recv(2) != bytes([VER, NO_AUTH]):
                return False
            s.sendall(bytes([VER, 0x03, 0x00, ATYP_IPV4]) + b"\x00" * 6)
            rep = s.recv(10)
            return len(rep) > 1 and rep[1] == 0x00
    except OSError:
        return False
