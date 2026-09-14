# phone-socks

USB로 연결한 안드로이드 폰의 **모바일 데이터를 노트북의 인터넷 회선으로** 사용합니다.
폰에서 SOCKS5 프록시 앱을 켜 두면, 노트북의 트래픽이 USB를 통해 그 앱으로 넘어가
폰의 모바일 데이터로 나갑니다.

테더링이 요금제에서 차단된 환경을 위해 만들었습니다. 통신사 입장에서는
폰 안의 앱 하나가 인터넷을 쓰는 것으로 보입니다.

## 왜 프록시 설정만으로는 부족한가

브라우저처럼 프록시 설정을 지원하는 프로그램은 `ALL_PROXY` 환경변수나
시스템 프록시 설정만으로 폰 회선을 쓰게 만들 수 있습니다. 하지만 프록시를
인식하지 못하는 프로그램(apt, docker, 게임, 각종 네이티브 앱)은 그대로
원래 회선으로 나갑니다.

phone-socks는 커널 방화벽(nftables)에서 나가는 트래픽을 가로채기 때문에
**프로그램이 프록시를 몰라도** 폰 회선을 쓰게 됩니다.

## 동작 구조

```
  앱  ──> nftables ──> 투명 프록시 ──> adb forward ──> 폰 SOCKS5 앱 ──> 모바일 데이터
          (가로챔)      (SOCKS5 변환)      (USB)
```

| 트래픽 | 처리 |
|---|---|
| TCP 전부 | 원래 목적지를 알아내 폰 SOCKS5로 중계 |
| DNS | SOCKS5는 UDP를 못 하므로 **TCP DNS로 변환**해 폰 경유 |
| QUIC 등 나머지 UDP | 차단 (안 막으면 원래 회선으로 새어나감) |
| 로컬/사설망 | 가로채지 않고 그대로 (LAN, localhost는 평소대로) |

## 설치

```bash
sudo apt install ./phone-socks_0.1.1_all.deb
```

직접 빌드하려면:

```bash
./packaging/build-deb.sh
```

## 준비

1. 폰에서 **USB 디버깅**을 켭니다. (설정 → 개발자 옵션)
2. 폰에서 SOCKS5 프록시 앱을 실행합니다. 기본 포트는 1080입니다.
3. USB로 연결하고 폰 화면에서 디버깅 허용을 확인합니다.
4. 모바일 데이터를 쓰려면 **폰의 Wi-Fi는 꺼 두세요.** 켜져 있으면 폰이
   Wi-Fi로 나가므로 모바일 데이터가 쓰이지 않습니다.

## 사용

GUI는 앱 목록에서 **Phone Socks**로 실행합니다. 스위치 하나로 켜고 끕니다.

명령줄:

```bash
phone-socks on           # 노트북 전체를 폰 회선으로
phone-socks off          # 원래 회선으로 복귀
phone-socks status       # 상태와 전송량
phone-socks test         # 현재 공인 IP 확인
```

### 선택한 앱만 폰 회선으로

```bash
phone-socks on --apps            # 앱 선택 모드로 켜기
phone-socks run firefox          # 이 앱만 폰 회선으로 실행
phone-socks adopt 12345          # 이미 실행 중인 프로세스를 폰 회선으로
phone-socks apps                 # 폰 회선을 쓰는 프로세스 목록
```

GUI에서는 **적용 범위**를 "선택한 앱만"으로 바꾸면 앱 목록이 나타납니다.
거기서 실행한 앱만 폰 회선을 씁니다.

## 설정

`/etc/phone-socks/config.json`

| 항목 | 설명 |
|---|---|
| `phone_socks_port` | 폰의 프록시 앱이 listen 중인 포트 (기본 1080) |
| `dns_upstream` | DNS 질의를 보낼 서버 |
| `block_udp_leak` | 중계 불가한 UDP를 차단할지. 끄면 원래 회선으로 샙니다 |
| `block_icmp_leak` | ping 등 ICMP도 차단할지 |
| `auto_reconnect` | USB 재연결 시 자동 복구 |

## 권한

GUI는 일반 사용자 권한으로 돌아갑니다. 네트워크 경로를 바꾸는 순간에만
polkit으로 인증을 받고, 실제 작업은 백그라운드 데몬이 합니다.

데몬은 root로 돕니다. polkit이 다른 프로세스의 권한을 확인하는 것을 uid 0인
호출자에게만 허용하기 때문입니다. 대신 root가 가질 수 있는 능력을 아래 셋으로
제한해 두었습니다.

- `CAP_NET_ADMIN` — 방화벽 규칙 조작
- `CAP_NET_RAW` — 소켓 리다이렉트
- `CAP_NET_BIND_SERVICE` — 내부 리스너 바인딩

`CAP_DAC_OVERRIDE`가 빠져 있어 파일 권한을 무시하지 못하고, `ProtectSystem=strict`,
`ProtectHome=yes`, `NoNewPrivileges=yes` 등으로 접근 범위를 더 좁혔습니다.

통신 내용을 들여다보거나 저장하지 않습니다.

## 알아둘 점

- **UDP는 중계되지 않습니다.** 폰의 SOCKS5 앱이 UDP ASSOCIATE를 지원하지
  않기 때문입니다. DNS는 TCP로 우회하고, QUIC은 차단하면 브라우저가 알아서
  TCP(HTTP/2)로 내려옵니다. UDP를 쓰는 화상통화나 일부 게임은 동작하지
  않을 수 있습니다.
- **속도는 USB와 폰 앱에 좌우됩니다.** 투명 프록시 자체는 파이썬으로
  구현돼 있어 매우 높은 처리량에서는 병목이 될 수 있습니다.
- **이미 열려 있는 연결**은 켜는 순간 바로 옮겨가지 않습니다. 연결 추적
  정보를 비우긴 하지만, 프로그램에 따라 재접속이 필요할 수 있습니다.
- **앱 선택 모드**에서 이미 실행 중인 앱은 `adopt`로 옮기거나 GUI에서
  다시 실행해야 합니다. 일부 앱(브라우저 등)은 기존 프로세스에 작업을
  넘기므로 완전히 종료한 뒤 실행해야 적용됩니다.

## 문제가 생겼을 때

인터넷이 끊겼다면 방화벽 규칙만 남았을 수 있습니다.

```bash
sudo nft delete table inet phonesocks
sudo systemctl restart phone-socks
```

데몬 로그:

```bash
journalctl -u phone-socks -f
```
# USBTether
# USBTether
