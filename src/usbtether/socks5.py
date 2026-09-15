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


def _dns_query(name: str, qtype: int) -> bytes:
    labels = b""
    for part in name.strip(".").split("."):
        raw = part.encode("idna")
        labels += bytes([len(raw)]) + raw
    labels += b"\x00"
    # 재귀 요청 플래그만 세운 최소 질의
    return (struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
            + labels + struct.pack(">HH", qtype, 1))


def _dns_answers(payload: bytes, qtype: int) -> list[str]:
    """응답에서 주소만 뽑는다. 이름 압축은 건너뛰기만 하면 되므로 따라가지 않는다."""
    if len(payload) < 12:
        return []
    qd, an = struct.unpack(">HH", payload[4:8])
    pos = 12
    for _ in range(qd):
        while pos < len(payload) and payload[pos]:
            if payload[pos] & 0xC0 == 0xC0:
                pos += 2
                break
            pos += payload[pos] + 1
        else:
            pos += 1
        pos += 4

    found = []
    for _ in range(an):
        if pos >= len(payload):
            break
        if payload[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while pos < len(payload) and payload[pos]:
                pos += payload[pos] + 1
            pos += 1
        if pos + 10 > len(payload):
            break
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", payload[pos:pos + 10])
        pos += 10
        data = payload[pos:pos + rdlen]
        pos += rdlen
        if rtype == qtype == 1 and rdlen == 4:
            found.append(socket.inet_ntop(socket.AF_INET, data))
        elif rtype == qtype == 28 and rdlen == 16:
            found.append(socket.inet_ntop(socket.AF_INET6, data))
    return found


def resolve_via_proxy(
    proxy_host: str,
    proxy_port: int,
    name: str,
    upstreams: list[str] | None = None,
    timeout: float = 8.0,
) -> list[str]:
    """프록시 너머에서 이름을 푼다.

    원래 회선의 이름 풀이가 막혀 있거나 다른 주소를 돌려주는 경우가 있어,
    프록시 반대편(폰)이 보는 주소를 받아야 한다.
    SOCKS5 는 UDP 를 나르지 못하므로 DNS 를 TCP 로 보낸다.
    """
    servers = upstreams or ["1.1.1.1"]
    found: list[str] = []
    for server in servers:
        for qtype in (1, 28):     # A, AAAA
            try:
                with socket.create_connection((proxy_host, proxy_port), timeout=timeout) as sock:
                    sock.settimeout(timeout)
                    sock.sendall(bytes([VER, 1, NO_AUTH]))
                    if sock.recv(2) != bytes([VER, NO_AUTH]):
                        break
                    sock.sendall(bytes([VER, CMD_CONNECT, 0x00])
                                 + _addr_payload(server, 53))
                    head = sock.recv(3)
                    if len(head) < 3 or head[1] != 0x00:
                        break
                    _skip_bound_addr_sync(sock)

                    query = _dns_query(name, qtype)
                    sock.sendall(struct.pack(">H", len(query)) + query)
                    size_raw = _recv_exactly(sock, 2)
                    payload = _recv_exactly(sock, struct.unpack(">H", size_raw)[0])
                    found.extend(_dns_answers(payload, qtype))
            except (OSError, ValueError, struct.error):
                continue
        if found:
            break
    return sorted(set(found))


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = b""
    while len(chunks) < count:
        piece = sock.recv(count - len(chunks))
        if not piece:
            raise OSError("연결이 일찍 닫혔습니다")
        chunks += piece
    return chunks


def _skip_bound_addr_sync(sock: socket.socket) -> None:
    atyp = _recv_exactly(sock, 1)[0]
    if atyp == ATYP_IPV4:
        _recv_exactly(sock, 4)
    elif atyp == ATYP_IPV6:
        _recv_exactly(sock, 16)
    elif atyp == ATYP_DOMAIN:
        _recv_exactly(sock, _recv_exactly(sock, 1)[0])
    else:
        raise OSError("알 수 없는 주소 형식")
    _recv_exactly(sock, 2)


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
