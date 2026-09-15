"""작업 표시줄 아이콘.

AppIndicator 는 GTK3 전용이라 GTK4 로 만든 창과 한 프로세스에 올릴 수 없다.
그래서 트레이만 따로 떼어 낸 작은 프로세스로 돈다.

창을 닫아도 이 프로세스가 남아 있어 아이콘으로 다시 열 수 있다.
터널 자체는 백그라운드 데몬이 유지하므로 둘 다 꺼져 있어도 연결은 그대로다.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

# 추상 유닉스 소켓으로 중복 실행을 막는다. 파일이 아니라서 찌꺼기가 남지 않는다.
LOCK_NAME = f"\0usbtether-tray-{os.getuid()}"


def is_running() -> bool:
    """트레이 프로세스가 이미 떠 있는지."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(LOCK_NAME)   # 묶이면 아무도 없다는 뜻
    except OSError:
        return True
    finally:
        probe.close()
    return False


def ensure_running() -> bool:
    """트레이가 없으면 띄운다."""
    if is_running():
        return True
    exe = "usbtether-tray"
    try:
        subprocess.Popen(
            [exe], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (FileNotFoundError, OSError):
        return False


def open_window() -> None:
    """창을 연다. 이미 떠 있으면 GApplication 이 그 창을 앞으로 올린다."""
    try:
        subprocess.Popen(
            ["usbtether-gui"], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    lock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        lock.bind(LOCK_NAME)
    except OSError:
        return 0        # 이미 떠 있다

    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ImportError, ValueError):
        print("트레이를 지원하지 않는 환경입니다 "
              "(gir1.2-ayatanaappindicator3-0.1 이 필요합니다).", file=sys.stderr)
        return 1
    from gi.repository import GLib, Gtk

    from . import client

    class Tray:
        def __init__(self) -> None:
            self.enabled = False
            self.indicator = AppIndicator.Indicator.new(
                "usbtether", "usbtether-off",
                AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("USBTether")
            self.indicator.set_menu(self._menu())
            # 가운데 클릭으로 바로 창이 열리게
            self.indicator.set_secondary_activate_target(self.open_item)
            self.refresh()
            GLib.timeout_add_seconds(5, self._tick)

        def _menu(self) -> Gtk.Menu:
            menu = Gtk.Menu()

            self.open_item = Gtk.MenuItem(label="창 열기")
            self.open_item.connect("activate", lambda *_: open_window())
            menu.append(self.open_item)

            self.state_item = Gtk.MenuItem(label="확인 중…")
            self.state_item.set_sensitive(False)
            menu.append(self.state_item)

            menu.append(Gtk.SeparatorMenuItem())

            self.toggle_item = Gtk.MenuItem(label="폰 회선 사용")
            self.toggle_item.connect("activate", self._on_toggle)
            menu.append(self.toggle_item)

            menu.append(Gtk.SeparatorMenuItem())

            quit_item = Gtk.MenuItem(label="종료")
            quit_item.connect("activate", self._on_quit)
            menu.append(quit_item)

            menu.show_all()
            return menu

        # -------------------------------------------------- 상태
        def _tick(self) -> bool:
            self.refresh()
            return True

        def refresh(self) -> None:
            try:
                data = client.status()
            except client.DaemonError as exc:
                self.enabled = False
                self.indicator.set_icon_full("usbtether-off", "USBTether")
                self.state_item.set_label(str(exc)[:60])
                self.toggle_item.set_sensitive(False)
                return

            self.enabled = bool(data.get("enabled"))
            self.toggle_item.set_sensitive(True)
            self.indicator.set_icon_full(
                "usbtether" if self.enabled else "usbtether-off", "USBTether"
            )

            problem = data.get("last_error") or ""
            if problem:
                summary = problem
            elif self.enabled:
                device = data.get("device") or {}
                name = device.get("model") or device.get("serial") or "폰"
                down = data.get("stats", {}).get("bytes_down", 0) / 1048576
                summary = f"{name} 경유 · 받은 양 {down:.0f} MB"
            else:
                summary = "꺼짐 — 원래 회선 사용 중"

            self.state_item.set_label(summary)
            self.toggle_item.set_label("폰 회선 끄기" if self.enabled else "폰 회선 사용")

        # -------------------------------------------------- 종료
        def _on_quit(self, *_args) -> None:
            """터널이 켜진 채로 종료하면 무슨 일이 벌어지는지 분명히 알린다.

            터널은 백그라운드 데몬이 유지하므로 트레이를 닫아도 계속 흐른다.
            그걸 모르고 자리를 뜨면 모바일 데이터가 계속 나간다.
            """
            if not self.enabled:
                Gtk.main_quit()
                return

            dialog = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                text="폰 회선을 계속 사용할까요?",
                secondary_text=(
                    "터널은 백그라운드 서비스가 유지하므로, 종료해도 노트북 "
                    "트래픽은 계속 폰의 모바일 데이터로 나갑니다."
                ),
            )
            dialog.add_button("취소", Gtk.ResponseType.CANCEL)
            dialog.add_button("계속 사용", Gtk.ResponseType.NO)
            dialog.add_button("끄고 종료", Gtk.ResponseType.YES)
            dialog.set_default_response(Gtk.ResponseType.YES)

            answer = dialog.run()
            dialog.destroy()

            if answer == Gtk.ResponseType.CANCEL:
                return
            if answer == Gtk.ResponseType.YES:
                try:
                    client.disable()
                except client.DaemonError:
                    pass
            Gtk.main_quit()

        # -------------------------------------------------- 동작
        def _on_toggle(self, *_args) -> None:
            self.toggle_item.set_sensitive(False)
            want = not self.enabled

            def work() -> None:
                error = ""
                try:
                    if want:
                        client.enable(mode="all")
                    else:
                        client.disable()
                except client.DaemonError as exc:
                    error = str(exc)
                GLib.idle_add(done, error)

            def done(error: str) -> bool:
                if error:
                    # 손댈 것이 있으면 창을 띄워 보여 준다
                    self.state_item.set_label(error[:60])
                    open_window()
                self.refresh()
                return False

            import threading
            threading.Thread(target=work, daemon=True).start()

    Tray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        lock.close()
    return 0
