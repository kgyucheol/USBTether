# USBTether

USB로 연결한 안드로이드 폰의 **모바일 데이터를 우분투 노트북의 인터넷 회선으로** 사용합니다.
폰에서 SOCKS5 프록시 앱을 켜 두면, 노트북의 모든 트래픽이 USB를 통해 그 앱으로
넘어가 폰의 모바일 데이터로 나갑니다.

테더링이 요금제에서 차단된 환경을 위해 만들었습니다. 통신사 입장에서는 폰 안의
앱 하나가 인터넷을 쓰는 것으로 보입니다.

> **In short** — USBTether routes your Ubuntu laptop's traffic through a SOCKS5
> proxy app running on a USB-connected Android phone, transparently. Every
> program is covered, including ones that have no proxy setting of their own.
> Built for plans where tethering is blocked. Korean documentation below.

<br>

## 프록시 설정과 뭐가 다른가

브라우저처럼 프록시 설정을 지원하는 프로그램은 환경변수 `ALL_PROXY` 나 GNOME
시스템 프록시 설정만으로도 폰 회선을 쓰게 만들 수 있습니다. 하지만 프록시를
모르는 프로그램 — `apt`, `docker`, `git`, 게임, 각종 네이티브 앱 — 은 그대로
원래 회선으로 나갑니다.

USBTether는 커널 방화벽(nftables)에서 나가는 트래픽을 가로챕니다. 프로그램이
프록시를 몰라도, 프록시 설정 자체가 없어도 폰 회선을 씁니다.

```
  앱 ──> nftables ──> 투명 프록시 ──> adb forward ──> 폰 SOCKS5 앱 ──> 모바일 데이터
         (가로챔)      (SOCKS5 변환)      (USB)
```

| 트래픽 | 처리 |
|---|---|
| TCP 전부 | 원래 목적지를 알아내 폰 SOCKS5로 중계 |
| DNS | SOCKS5는 UDP를 못 하므로 **TCP DNS로 변환**해 폰 경유 |
| QUIC 등 나머지 UDP | 차단 (안 막으면 원래 회선으로 새어나감) |
| 로컬 / 사설망 | 가로채지 않음. LAN·localhost는 평소대로 |

<br>

## 준비물

| | |
|---|---|
| 노트북 | 우분투 24.04 이상, GNOME 데스크톱 |
| 폰 | 안드로이드, USB 디버깅 가능 |
| 폰 앱 | SOCKS5 서버를 여는 프록시 앱 |
| 케이블 | 데이터 전송이 되는 USB 케이블 (충전 전용 케이블은 안 됩니다) |

<br>

## 설정 — 폰

### 1. 개발자 옵션과 USB 디버깅 켜기

`설정` → `휴대전화 정보` → `소프트웨어 정보` → **빌드 번호**를 7번 연속 탭하면
개발자 옵션이 나타납니다. 그다음 `설정` → `개발자 옵션` → **USB 디버깅**을 켭니다.

### 2. Every Proxy 설치

Play 스토어에서 **[Every Proxy](https://play.google.com/store/apps/details?id=com.gorillasoftware.everyproxy)**
를 설치합니다.

앱을 열고 **SOCKS Proxy 토글만 켜면** 됩니다. 포트는 기본값 1080 그대로 두세요.
그 외에 건드릴 설정은 없습니다.

> 같은 화면의 HTTP Proxy 가 아니라 **SOCKS Proxy** 를 켜야 합니다. HTTP 프록시는
> 웹 트래픽만 이해하므로 SSH·게임·패키지 관리자 같은 것들이 통과하지 못합니다.

### 3. 모바일 데이터로 나가게 하기

폰의 **Wi-Fi를 끕니다.** Wi-Fi가 켜져 있으면 폰이 Wi-Fi로 나가므로 모바일
데이터가 쓰이지 않습니다. USBTether의 상태 화면에 폰이 어떤 회선을 쓰는지
표시되니 확인할 수 있습니다.

<br>

## 설정 — 노트북

### 1. 설치

[Releases](../../releases) 에서 `.deb` 를 받아 설치합니다.

```bash
sudo apt install ./usbtether_0.6.1_all.deb
```

필요한 의존성(`adb`, `nftables`, GTK4, polkit 등)은 apt가 함께 설치합니다.
설치가 끝나면 백그라운드 서비스가 자동으로 시작되고 부팅 시에도 켜집니다.

### 2. USB 장치 접근 권한

`adb` 가 일반 사용자 권한으로 폰에 접근하려면 `plugdev` 그룹에 속해야 합니다.

```bash
groups | grep -q plugdev || sudo usermod -aG plugdev "$USER"
```

이 명령으로 그룹에 추가했다면 **로그아웃 후 다시 로그인**해야 적용됩니다.
udev 규칙은 의존성으로 설치되는 `android-sdk-platform-tools-common` 이 제공합니다.

### 3. 폰 연결하고 디버깅 승인

USB로 연결하면 폰 화면에 *"USB 디버깅을 허용하시겠습니까?"* 가 뜹니다.
**항상 허용**에 체크하고 허용을 누릅니다. 체크하지 않으면 케이블을 다시 꽂을
때마다 승인해야 하고, 승인이 풀린 동안에는 연결이 끊깁니다.

확인:

```bash
adb devices
# R5CY60DWRBK    device      <- 'device' 라고 나와야 합니다
#                             'unauthorized' 면 폰 화면의 승인을 놓친 것입니다
```

### 4. 켜기

앱 목록에서 **USBTether** 를 실행하고 스위치를 켭니다. 네트워크 경로를 바꾸는
동작이라 처음 한 번 polkit 인증 창이 뜹니다.

터미널을 선호한다면:

```bash
usbtether on
```

<br>

## 사용법

### 기본

```bash
usbtether on        # 노트북 전체를 폰 회선으로
usbtether off       # 원래 회선으로 복귀
usbtether status    # 상태, 전송량, 연결 수
usbtether test      # 현재 공인 IP 확인
```

GUI에서는 스위치 하나로 켜고 끕니다. 폰 기종, 현재 회선(모바일 데이터 / Wi-Fi),
공인 IP, 주고받은 양이 함께 표시됩니다.

### 안 되는 곳만 폰 회선으로 (데이터 절약)

원래 회선이 대체로 잘 되는데 **일부만 접근이 막히는** 환경을 위한 모드입니다.
사내망, 공용 Wi-Fi, 지역 차단처럼 이유는 여러 가지일 수 있습니다.

```bash
usbtether on --split
```

기본은 원래 회선입니다. 처음 보는 목적지는 원래 회선으로 먼저 시도하고,
연결이 안 될 때만 폰으로 재시도합니다. 한 번 판정된 곳은 기억해 두므로
다음부터는 곧장 맞는 길로 갑니다.

```
사설망·LAN 주소        손대지 않음
직결로 잘 되던 곳       커널에서 그대로 통과 (앱을 거치지 않음)
막힌 것으로 판정된 곳   바로 폰으로
처음 보는 곳           원래 회선 먼저 → 실패하면 폰으로 재시도하고 기억
```

**이름 풀이는 건드리지 않습니다.** 원래 회선의 DNS 를 그대로 쓰므로, 그 망에서만
풀리는 이름(사내 서버 등)이 계속 동작합니다.

자동 판정이 놓치는 경우가 있습니다. 차단 페이지가 정상 응답으로 돌아오는
방식이라면 연결 자체는 성공하므로 막힌 줄 모릅니다. 그런 곳은 직접 지정합니다.

```bash
usbtether split                      # 목록 보기
usbtether split add example.com      # 항상 폰으로 보낼 곳 추가
usbtether split remove example.com
usbtether split forget               # 자동 학습 기록 지우기
```

직접 지정한 도메인은 **폰을 통해 이름을 풉니다.** 원래 회선의 DNS 가 이미
막혀 있거나 다른 주소를 돌려주는 경우가 있기 때문입니다.

GUI 에서는 **적용 범위**를 "안 되는 곳만"으로 바꾸면 목록이 나타납니다.

### 선택한 앱만 폰 회선으로

노트북 전체가 아니라 특정 앱만 폰 데이터를 쓰게 할 수 있습니다.

```bash
usbtether on --apps          # 앱 선택 모드로 켜기
usbtether run firefox        # 이 앱만 폰 회선으로 실행
usbtether adopt 12345        # 이미 실행 중인 프로세스를 폰 회선으로 옮기기
usbtether apps               # 폰 회선을 쓰는 프로세스 목록
```

GUI에서는 **적용 범위**를 "선택한 앱만"으로 바꾸면 앱 목록이 나타납니다.
거기서 실행한 앱만 폰 회선을 씁니다.

내부적으로는 선택한 앱을 전용 cgroup 에 모으고, 방화벽이 그 cgroup 에서 나온
트래픽만 폰으로 보냅니다.

### 창을 닫아도 연결은 유지됩니다

터널을 유지하는 것은 창이나 작업 표시줄 아이콘이 아니라 백그라운드 서비스입니다.
창을 닫았다고 연결이 끊기면 받고 있던 파일이 전부 죽기 때문에 일부러 분리해
두었습니다.

| 동작 | 결과 |
|---|---|
| 창의 X 를 누름 | 창만 숨겨짐. 아이콘을 누르면 다시 열림 |
| 아이콘에서 종료 | 터널을 끌지 계속 쓸지 물어봅니다 |
| `usbtether off` | 터널이 꺼지고 원래 회선으로 복귀 |
| 재부팅 | 터널은 꺼진 상태로 시작합니다 |

즉 **앱을 다 닫아도 모바일 데이터는 계속 나갈 수 있습니다.** 확실히 끄려면
아이콘 메뉴에서 종료하며 "끄고 종료"를 고르거나, `usbtether off` 를 실행하세요.
지금 켜져 있는지는 언제든 `usbtether status` 로 확인할 수 있습니다.

### 데이터 절약 — 자동 업데이트 보류

우분투는 설치 직후부터 보안 업데이트를 알아서 받아 설치합니다. 모바일 데이터로
나가는 중에 이게 돌면 자는 사이에 수백 MB 가 빠져나갑니다. snap 도 기본적으로
4시간마다 갱신을 확인합니다.

USBTether 를 켜면 이런 **예약된 자동 실행만** 보류하고, 끄면 정확히 되돌립니다.

```bash
usbtether updates    # 이 시스템에서 찾아낸 자동 업데이트 작업과 보류 여부
```

목록을 미리 정해 두지 않습니다. 시스템마다 깔린 것이 다르기 때문에, 설치된
systemd 타이머를 훑어 업데이트성 작업을 찾아냅니다. GUI 의 **데이터 절약**
항목을 펼치면 찾아낸 것이 그대로 나오고, 각각 끄고 켤 수 있습니다.

되돌릴 때는 **실제로 바꾼 것만** 되돌립니다. 원래 꺼져 있던 타이머는 건드리지
않으므로, 일부러 꺼 둔 것이 마음대로 켜지지 않습니다.

> **사람이 직접 시작한 일은 막지 않습니다.** `sudo apt upgrade`, `snap refresh`,
> 앱센터의 업데이트 버튼은 그대로 동작합니다. 보류되는 것은 예약된 자동 실행뿐이라,
> 모바일 회선으로 업데이트를 받고 싶다면 그냥 직접 실행하면 됩니다.

관련 설정은 `block_auto_updates` (기능 자체를 끄기) 와 `update_holds`
(`["auto"]` 면 찾아낸 것 전부, 아니면 보류할 항목만 나열) 입니다.

### Wi-Fi 를 꺼도 됩니다

방화벽이 트래픽을 가로채려면 커널이 먼저 그 패킷을 보낼 경로를 찾아야 합니다.
Wi-Fi 를 끄면 기본 경로가 사라져 앱의 `connect()` 가 곧바로 실패하고, 가로챌
패킷 자체가 생기지 않습니다. 데이터는 USB 로 나가는데도 Wi-Fi 가 필요해지는
셈입니다.

그래서 켤 때 `usbt0` 더미 인터페이스에 아주 낮은 우선순위(metric 30000)의 기본
경로를 깔아 둡니다. 실제 회선이 있으면 그쪽이 쓰이고, 없으면 이 경로가 패킷을
받아 방화벽까지 흘려보냅니다. Wi-Fi 와 함께 사라지는 DNS 서버 주소도 이
인터페이스에 붙여 둡니다. 끄면 인터페이스째 지워집니다.

<br>

## 설정 파일

`/etc/usbtether/config.json`

| 항목 | 기본값 | 설명 |
|---|---|---|
| `phone_socks_port` | `1080` | 폰의 프록시 앱이 listen 중인 포트 |
| `local_socks_port` | `1080` | 노트북에서 쓸 로컬 포트 |
| `device_serial` | `""` | 여러 대 연결 시 쓸 기기 (`adb devices` 의 시리얼) |
| `dns_upstream` | `1.1.1.1`, `8.8.8.8` | DNS 질의를 보낼 서버 |
| `block_udp_leak` | `true` | 중계 불가한 UDP 차단. 끄면 원래 회선으로 샙니다 |
| `block_icmp_leak` | `false` | ping 등 ICMP 도 차단할지 |
| `auto_reconnect` | `true` | USB 재연결 시 자동 복구 |
| `split_targets` | `[]` | "안 되는 곳만" 모드에서 항상 폰으로 보낼 도메인·IP |
| `direct_timeout` | `4.0` | 원래 회선을 몇 초 기다렸다 막힌 것으로 볼지 |
| `block_auto_updates` | `true` | 켜져 있는 동안 예약된 자동 업데이트 보류 |
| `update_holds` | `["auto"]` | 보류할 대상. `auto` 면 찾아낸 것 전부 |
| `auto_disable_on_loss` | `true` | 폰이 오래 끊기면 스스로 꺼져 원래 회선 복귀 |
| `loss_grace_seconds` | `30` | 자동 해제까지 기다리는 시간 |

고친 뒤 적용:

```bash
sudo systemctl restart usbtether
```

<br>

## 권한

GUI는 일반 사용자 권한으로 돕니다. 네트워크 경로를 바꾸는 순간에만 polkit 으로
인증을 받고, 실제 작업은 백그라운드 데몬이 합니다.

데몬은 root 로 돕니다. polkit 이 다른 프로세스의 권한을 확인하는 것을 uid 0 인
호출자에게만 허용하기 때문입니다. 대신 root 가 가질 수 있는 능력을 셋으로
제한해 두었습니다.

- `CAP_NET_ADMIN` — 방화벽 규칙 조작
- `CAP_NET_RAW` — 소켓 리다이렉트
- `CAP_NET_BIND_SERVICE` — 내부 리스너 바인딩

`CAP_DAC_OVERRIDE` 가 빠져 있어 파일 권한을 무시하지 못하고, `ProtectSystem=strict`,
`ProtectHome=yes`, `NoNewPrivileges=yes` 로 접근 범위를 더 좁혔습니다.

통신 내용을 들여다보거나 저장하지 않습니다.

<br>

## 알아둘 점

- **UDP 는 중계되지 않습니다.** 폰의 SOCKS5 앱이 UDP ASSOCIATE 를 지원하지 않기
  때문입니다. DNS 는 TCP 로 우회하고, QUIC 은 차단하면 브라우저가 알아서
  TCP(HTTP/2) 로 내려옵니다. UDP 를 쓰는 화상통화나 일부 게임은 동작하지 않습니다.
- **속도는 USB 와 폰 앱에 좌우됩니다.** 투명 프록시는 파이썬으로 구현돼 있어
  아주 높은 처리량에서는 병목이 될 수 있습니다.
- **이미 열려 있는 연결**은 켜는 순간 바로 옮겨가지 않습니다. 연결 추적 정보를
  비우긴 하지만 프로그램에 따라 재접속이 필요할 수 있습니다.
- **앱 선택 모드**에서 이미 실행 중인 앱은 `adopt` 로 옮기거나 GUI 에서 다시
  실행해야 합니다. 브라우저처럼 기존 프로세스에 작업을 넘기는 앱은 완전히 종료한
  뒤 실행해야 적용됩니다.
- **데이터 요금**에 주의하세요. 켜는 순간 노트북 전체 트래픽이 모바일 데이터로
  나갑니다.

<br>

## 문제 해결

### `adb devices` 가 `unauthorized` 로 나옴

폰 화면의 USB 디버깅 승인을 놓친 것입니다. 케이블을 다시 꽂고 폰 화면에서
허용하세요. 승인 창이 안 뜨면 `개발자 옵션` → `USB 디버깅 승인 취소` 를 누른 뒤
다시 연결합니다.

### 폰을 못 찾음

```bash
adb devices                       # 목록에 나오는지
groups | grep plugdev             # 그룹에 속해 있는지 (없으면 위 설정 2번)
```

충전 전용 USB 케이블인지도 확인하세요.

### 켜지긴 하는데 인터넷이 안 됨

폰의 프록시 앱이 실제로 SOCKS5 를 열고 있는지 확인합니다.

```bash
usbtether status                  # '폰 SOCKS 앱' 항목 확인
adb shell netstat -tln | grep 1080
```

포트가 다르면 `/etc/usbtether/config.json` 의 `phone_socks_port` 를 바꿉니다.

### 인터넷이 끊긴 채로 남음

방화벽 규칙만 남은 경우입니다. 이 한 줄로 즉시 복구됩니다.

```bash
sudo nft delete table inet usbtether && sudo ip link del usbt0
sudo systemctl restart usbtether
```

데몬은 어떤 경로로 죽든 규칙을 지우도록 만들어져 있고, 폰이 30초 넘게 끊기면
스스로 꺼져 원래 회선을 돌려줍니다.

### 로그 보기

```bash
journalctl -u usbtether -f
```

<br>

## 소스에서 빌드

외부 소스 의존성은 없습니다. 파이썬 표준 라이브러리와 시스템에 설치된
PyGObject 만 씁니다.

```bash
git clone https://github.com/kgyucheol/usbtether.git
cd usbtether
./packaging/build-deb.sh
sudo apt install ./build/usbtether_*_all.deb
```

빌드에는 `dpkg-dev` 만 있으면 됩니다. debhelper 는 쓰지 않습니다.

### 구조

```
src/usbtether/
  proxy.py      투명 프록시 엔진 — TCP 가로채기, DNS 를 TCP 로 변환
  firewall.py   nftables 규칙 생성·적용. 전용 테이블만 쓰고 통째로 지운다
  route.py      Wi-Fi 없이도 동작하게 하는 더미 기본 경로
  appsel.py     앱별 라우팅 (cgroup v2)
  saver.py      데이터 절약 — 예약된 자동 업데이트 탐색·보류·원복
  adb.py        USB 연결과 폰 상태
  socks5.py     SOCKS5 클라이언트
  service.py    특권 데몬 — 유닉스 소켓 + polkit 인증
  client.py     데몬 호출 클라이언트
  cli.py        명령줄
  gui.py        GTK4 / libadwaita 창
  tray.py       작업 표시줄 아이콘 (AppIndicator 가 GTK3 전용이라 별도 프로세스)
```

<br>

## 라이선스

GPL-3.0. 자세한 내용은 [LICENSE](LICENSE) 를 보세요.
