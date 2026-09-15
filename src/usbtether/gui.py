"""GTK4 / libadwaita GUI."""

from __future__ import annotations

import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__, appsel, client, tray  # noqa: E402

APP_ID = "org.usbtether.Gui"


def run_async(work, on_done):
    """느린 호출(데몬 통신, IP 조회)을 UI 밖에서 돌린다."""
    def runner():
        try:
            result, error = work(), None
        except Exception as exc:  # 데몬 오류를 그대로 화면에 보여준다
            result, error = None, exc
        GLib.idle_add(on_done, result, error)
    threading.Thread(target=runner, daemon=True).start()


def _notify(title: str, body: str) -> None:
    """데스크톱 알림. 실패해도 앱 동작에는 지장이 없다."""
    try:
        subprocess.run(
            ["notify-send", "--app-name=USBTether", "--icon=usbtether", title, body],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def public_ip() -> tuple[str, bool]:
    """(표시할 문자열, 성공 여부). 실패해도 왜 실패했는지는 호출부가 판단한다."""
    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "10", "--noproxy", "*", "https://ifconfig.me"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", False
    address = proc.stdout.strip()
    return (address, True) if address else ("", False)


class AppChooser(Adw.Window):
    """설치된 앱 중 하나를 골라 폰 회선으로 실행한다."""

    def __init__(self, parent: Gtk.Window, on_pick):
        super().__init__(title="앱 선택", transient_for=parent, modal=True,
                         default_width=420, default_height=560)
        self.on_pick = on_pick

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        box.append(header)

        self.search = Gtk.SearchEntry(placeholder_text="앱 이름 검색")
        self.search.set_margin_top(8)
        self.search.set_margin_bottom(8)
        self.search.set_margin_start(12)
        self.search.set_margin_end(12)
        self.search.connect("search-changed", lambda *_: self._refill())
        box.append(self.search)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_start(12)
        self.listbox.set_margin_end(12)
        self.listbox.set_margin_bottom(12)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.listbox)
        box.append(scroller)
        self.set_content(box)

        self.apps = appsel.list_desktop_apps()
        self._refill()

    def _refill(self) -> None:
        needle = self.search.get_text().strip().lower()
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt

        for app in self.apps:
            if needle and needle not in app["name"].lower():
                continue
            row = Adw.ActionRow(title=app["name"], subtitle=app["exec"] or "")
            if app["icon"]:
                icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string(app["icon"]))
                icon.set_pixel_size(32)
                row.add_prefix(icon)
            button = Gtk.Button(label="실행", valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", self._launch, app)
            row.add_suffix(button)
            self.listbox.append(row)

    def _launch(self, _button, app) -> None:
        self.on_pick(app)
        self.close()


class MainWindow(Adw.ApplicationWindow):
    IP_RETRIES = 3

    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="USBTether",
                         default_width=520, default_height=680)
        # 창을 닫아도 프로그램은 트레이에 남는다
        self.connect("close-request", self._on_close_request)
        self._busy = False
        self._ip = "—"
        self._last_banner = ""
        self._suppress_config = False
        self._holds: list[str] = ["auto"]
        self._ticks = 0
        self._warned_on_hide = False
        self._build()
        self.refresh()
        self._load_updaters()
        GLib.timeout_add_seconds(3, self._tick)

    # ------------------------------------------------------------ 화면 구성
    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda *_: self.switch_row.set_active(False))

        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("새로고침", "win.refresh")
        menu.append("정보", "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)
        outer.append(header)
        outer.append(self.banner)

        page = Adw.PreferencesPage()

        # 연결 ----------------------------------------------------
        group = Adw.PreferencesGroup(title="연결")
        self.switch_row = Adw.SwitchRow(
            title="폰 회선 사용",
            subtitle="켜면 노트북 트래픽이 폰의 모바일 데이터로 나갑니다",
        )
        self.switch_row.connect("notify::active", self._on_toggle)
        group.add(self.switch_row)

        self.device_row = Adw.ActionRow(title="폰", subtitle="확인 중…")
        self.device_row.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic"))
        group.add(self.device_row)

        self.ip_row = Adw.ActionRow(title="공인 IP", subtitle="—")
        self.ip_row.add_prefix(Gtk.Image.new_from_icon_name("network-workgroup-symbolic"))
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", lambda *_: self._refresh_ip())
        self.ip_row.add_suffix(refresh_btn)
        group.add(self.ip_row)
        page.add(group)

        # 적용 범위 ------------------------------------------------
        scope = Adw.PreferencesGroup(
            title="적용 범위",
            description="어떤 프로그램이 폰 회선을 쓸지 정합니다",
        )
        self.mode_row = Adw.ComboRow(
            title="대상",
            model=Gtk.StringList.new(
                ["노트북 전체", "선택한 앱만", "안 되는 곳만"]
            ),
            subtitle="'안 되는 곳만'은 원래 회선을 먼저 쓰고 실패한 곳만 폰으로 보냅니다",
        )
        self.mode_row.connect("notify::selected", self._on_mode_change)
        scope.add(self.mode_row)
        page.add(scope)

        # 막힌 곳 목록 ----------------------------------------------
        self.split_group = Adw.PreferencesGroup(
            title="폰으로 보낼 곳",
            description="원래 회선으로 되는 곳은 그대로 두고, 안 되는 곳만 폰을 씁니다",
        )
        add_target = Adw.EntryRow(title="주소 추가 (도메인 또는 IP)")
        add_target.connect("entry-activated", self._on_add_target)
        add_target.set_show_apply_button(True)
        add_target.connect("apply", self._on_add_target)
        self.split_group.add(add_target)
        self._target_entry = add_target

        self.split_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.split_list.add_css_class("boxed-list")
        self.split_list.set_margin_top(8)
        self.split_group.add(self.split_list)

        self.learned_row = Adw.ActionRow(
            title="자동으로 찾아낸 곳", subtitle="아직 없음"
        )
        forget = Gtk.Button(label="잊기", valign=Gtk.Align.CENTER)
        forget.add_css_class("flat")
        forget.connect("clicked", self._on_forget)
        self.learned_row.add_suffix(forget)
        self.split_group.add(self.learned_row)
        page.add(self.split_group)

        # 데이터 절약 ----------------------------------------------
        saving = Adw.PreferencesGroup(
            title="데이터 절약",
            description="직접 실행하는 업데이트는 막지 않습니다. "
                        "예약된 자동 실행만 켜져 있는 동안 보류합니다.",
        )
        self.updates_row = Adw.ExpanderRow(
            title="자동 업데이트 보류",
            subtitle="확인 중…",
            show_enable_switch=True,
        )
        self.updates_row.connect("notify::enable-expansion", self._on_updates_toggle)
        saving.add(self.updates_row)
        page.add(saving)
        self._updater_rows: dict[str, Adw.SwitchRow] = {}
        self._updaters: list[dict] = []

        # 앱 목록 --------------------------------------------------
        self.apps_group = Adw.PreferencesGroup(
            title="폰 회선을 쓰는 앱",
            description="여기서 실행한 앱만 폰 회선으로 나갑니다",
        )
        add_row = Adw.ActionRow(
            title="앱 추가해서 실행",
            subtitle="목록에서 고르면 폰 회선으로 새로 실행됩니다",
            activatable=True,
        )
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.connect("activated", lambda *_: self._open_chooser())
        self.apps_group.add(add_row)

        self.running_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.running_box.add_css_class("boxed-list")
        self.running_box.set_margin_top(8)
        self.apps_group.add(self.running_box)
        page.add(self.apps_group)

        # 통계 ----------------------------------------------------
        self.stats_group = Adw.PreferencesGroup(title="전송량")
        self.traffic_row = Adw.ActionRow(title="주고받은 양", subtitle="—")
        self.conn_row = Adw.ActionRow(title="연결", subtitle="—")
        self.dns_row = Adw.ActionRow(title="DNS 질의", subtitle="—")
        for row in (self.traffic_row, self.conn_row, self.dns_row):
            self.stats_group.add(row)
        page.add(self.stats_group)

        outer.append(page)
        self.toasts.set_child(outer)
        self.set_content(self.toasts)

        for name, handler in (
            ("refresh", lambda *_: self.refresh()),
            ("about", lambda *_: self._about()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    # ------------------------------------------------------------ 동작
    def _on_close_request(self, *_args) -> bool:
        """X 를 눌러도 끄지 않고 숨긴다. 작업 표시줄 아이콘으로 다시 연다.

        트레이가 없는 환경에서까지 숨기면 창을 되살릴 방법이 없으므로,
        그럴 때는 평소처럼 닫는다.
        """
        if tray.is_running():
            self.set_visible(False)
            # 창을 닫았다고 연결이 끊기는 게 아니라는 걸 한 번은 알려 준다.
            # 모르고 자리를 뜨면 모바일 데이터가 계속 나간다.
            if not self._warned_on_hide:
                self._warned_on_hide = True
                _notify(
                    "USBTether 는 계속 실행 중입니다",
                    "창을 닫아도 폰 회선은 그대로 사용됩니다. "
                    "끄려면 작업 표시줄 아이콘을 누르세요.",
                )
            return True   # 기본 동작(창 파괴)을 막는다
        return False

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def _on_toggle(self, row, _param) -> None:
        if self._busy:
            return
        want_on = row.get_active()
        self._busy = True
        row.set_sensitive(False)

        if want_on:
            mode = {1: "apps", 2: "split"}.get(self.mode_row.get_selected(), "all")
            cgroup = appsel.ensure_slice() if mode == "apps" else None
            work = lambda: client.enable(mode=mode, cgroup_path=cgroup)
        else:
            def work():
                result = client.disable()
                appsel.release_slice()
                return result

        def done(result, error):
            self._busy = False
            row.set_sensitive(True)
            if error:
                self._toast(str(error))
                row.set_active(not want_on)
            else:
                self._toast("폰 회선을 사용합니다" if want_on else "원래 회선으로 돌아왔습니다")
                self._refresh_ip(1)
            self.refresh()
            return False

        run_async(work, done)

    def _on_updates_toggle(self, row, _param) -> None:
        if self._suppress_config:
            return
        want = row.get_enable_expansion()

        def done(_result, error):
            if error:
                self._toast(str(error))
            else:
                self._toast("예약된 자동 업데이트를 보류합니다" if want
                            else "자동 업데이트가 평소대로 돌아갑니다")
            self.refresh()
            return False

        run_async(lambda: client.set_config(block_auto_updates=want), done)

    def _on_updater_item_toggle(self, row, _param) -> None:
        """개별 항목을 끄면 '전부'에서 명시 목록으로 바뀐다."""
        if self._suppress_config:
            return
        chosen = [
            item_id for item_id, switch in self._updater_rows.items()
            if switch.get_active()
        ]

        def done(_result, error):
            if error:
                self._toast(str(error))
            return False

        run_async(lambda: client.set_config(update_holds=chosen or []), done)

    def _load_updaters(self) -> None:
        """이 시스템에 실제로 있는 자동 업데이트 작업을 찾아 목록을 그린다."""
        def done(result, error):
            if error or not result:
                self.updates_row.set_subtitle("목록을 읽지 못했습니다")
                return False
            self._updaters = result.get("items", [])
            self._build_updater_rows()
            return False

        run_async(client.updaters, done)

    def _build_updater_rows(self) -> None:
        for switch in self._updater_rows.values():
            self.updates_row.remove(switch)
        self._updater_rows.clear()

        if not self._updaters:
            self.updates_row.set_subtitle("보류할 자동 업데이트를 찾지 못했습니다")
            return

        self._suppress_config = True
        for item in self._updaters:
            row = Adw.SwitchRow(title=item["name"], subtitle=item["detail"])
            row.set_active(self._is_held(item["id"]))
            row.connect("notify::active", self._on_updater_item_toggle)
            self.updates_row.add_row(row)
            self._updater_rows[item["id"]] = row
        self._suppress_config = False
        self.updates_row.set_subtitle(f"{len(self._updaters)}개 항목을 찾았습니다")

    def _is_held(self, item_id: str) -> bool:
        holds = self._holds
        return not holds or "auto" in holds or item_id in holds

    def _on_add_target(self, row, *_args) -> None:
        value = row.get_text().strip()
        if not value:
            return
        row.set_text("")

        def work():
            targets = list(client.status()["config"].get("split_targets", []))
            if value not in targets:
                targets.append(value)
            return client.set_config(split_targets=targets)

        def done(_result, error):
            self._toast(str(error) if error else f"{value} 추가됨 — 껐다 켜면 적용됩니다")
            self.refresh()
            return False

        run_async(work, done)

    def _on_remove_target(self, _button, value: str) -> None:
        def work():
            targets = [t for t in client.status()["config"].get("split_targets", [])
                       if t != value]
            return client.set_config(split_targets=targets)

        def done(_result, error):
            self._toast(str(error) if error else f"{value} 제거됨")
            self.refresh()
            return False

        run_async(work, done)

    def _on_forget(self, _button) -> None:
        def done(_result, error):
            self._toast(str(error) if error else "자동 학습 기록을 지웠습니다")
            self.refresh()
            return False

        run_async(client.split_forget, done)

    def _refresh_split(self, data: dict) -> None:
        targets = data.get("config", {}).get("split_targets", [])
        child = self.split_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.split_list.remove(child)
            child = nxt

        if not targets:
            empty = Adw.ActionRow(title="직접 지정한 곳 없음",
                                  subtitle="자동 판정만으로 동작합니다")
            empty.set_sensitive(False)
            self.split_list.append(empty)
        else:
            for value in targets:
                row = Adw.ActionRow(title=value)
                remove = Gtk.Button(icon_name="user-trash-symbolic",
                                    valign=Gtk.Align.CENTER)
                remove.add_css_class("flat")
                remove.connect("clicked", self._on_remove_target, value)
                row.add_suffix(remove)
                self.split_list.append(row)

        count = data.get("blocked_count", 0)
        self.learned_row.set_subtitle(
            f"{count}곳 — 원래 회선으로 연결이 안 되던 주소" if count else "아직 없음"
        )

    def _on_mode_change(self, row, _param) -> None:
        apps_mode = row.get_selected() == 1
        self.apps_group.set_visible(apps_mode)
        self.split_group.set_visible(row.get_selected() == 2)
        if self.switch_row.get_active() and not self._busy:
            self._toast("바뀐 범위는 껐다 켜면 적용됩니다")

    def _open_chooser(self) -> None:
        def picked(app):
            appsel.ensure_slice()
            ok, err = appsel.launch(app["exec"], app.get("desktop_file") or None)
            self._toast(f"{app['name']} 실행됨" if ok else f"실행 실패: {err}")
            GLib.timeout_add_seconds(2, lambda: (self.refresh(), False)[1])
        AppChooser(self, picked).present()

    def _refresh_ip(self, attempt: int = 1) -> None:
        self.ip_row.set_subtitle("확인 중…")

        def done(result, error):
            address, ok = ("", False) if error else result
            if ok:
                self._ip = address
                self.ip_row.set_subtitle(address)
                return False

            # 켜는 직후에는 폰 쪽 통로가 자리잡는 데 몇 초 걸린다. 몇 번 더 해 본다.
            if attempt < self.IP_RETRIES:
                self.ip_row.set_subtitle(f"확인 중… (재시도 {attempt}/{self.IP_RETRIES})")
                GLib.timeout_add_seconds(
                    4, lambda: (self._refresh_ip(attempt + 1), False)[1]
                )
            else:
                self._ip = "확인 실패"
                self.ip_row.set_subtitle("확인 실패 — 폰의 SOCKS 앱과 USB 연결을 확인하세요")
            return False

        run_async(public_ip, done)

    def _tick(self) -> bool:
        # 창이 숨어 있으면 자주 물어볼 이유가 없다. adb 호출이 매번 따라붙는다.
        self._ticks += 1
        interval = 1 if self.get_visible() else 5
        if not self._busy and self._ticks % interval == 0:
            self.refresh()
        return True

    def refresh(self) -> None:
        def done(data, error):
            if error:
                self.device_row.set_subtitle(str(error))
                self.switch_row.set_sensitive(False)
                return False
            self.switch_row.set_sensitive(not self._busy)
            self._apply_status(data)
            return False

        run_async(client.status, done)

    def _apply_status(self, data: dict) -> None:
        enabled = data["enabled"]
        if self.switch_row.get_active() != enabled and not self._busy:
            self._busy = True
            self.switch_row.set_active(enabled)
            self._busy = False

        device = data.get("device")
        if device:
            label = device.get("model") or device["serial"]
            transport = data.get("transport") or ""
            readable = {"CELLULAR": "모바일 데이터", "WIFI": "Wi-Fi"}.get(transport, transport)
            self.device_row.set_subtitle(f"{label} · {readable}" if readable else label)
        else:
            self.device_row.set_subtitle("USB로 연결된 폰이 없습니다")

        problem = data.get("last_error") or ""
        if problem:
            self.device_row.set_subtitle(problem)
        if problem != self._last_banner:
            self._last_banner = problem
            self.banner.set_title(problem)
            self.banner.set_revealed(bool(problem))
        # 빈 문자열을 주면 라벨 없는 버튼이 남는다. 끌 게 없으면 버튼 자체를 없앤다.
        self.banner.set_button_label("끄기" if enabled and problem else None)

        if not enabled:
            self.ip_row.set_subtitle(self._ip)

        mode_index = {"apps": 1, "split": 2}.get(data.get("mode"), 0)
        if enabled and self.mode_row.get_selected() != mode_index:
            self.mode_row.set_selected(mode_index)
        self.apps_group.set_visible(self.mode_row.get_selected() == 1)
        self.split_group.set_visible(self.mode_row.get_selected() == 2)
        self._refresh_split(data)

        config = data.get("config", {})
        self._holds = config.get("update_holds", ["auto"])
        wanted = bool(config.get("block_auto_updates", True))
        if self.updates_row.get_enable_expansion() != wanted:
            self._suppress_config = True
            self.updates_row.set_enable_expansion(wanted)
            self._suppress_config = False
        if enabled and wanted and data.get("updates_blocked"):
            self.updates_row.set_subtitle("보류 중 — 끄면 원래대로 돌아갑니다")
        elif self._updaters:
            self.updates_row.set_subtitle(f"{len(self._updaters)}개 항목을 찾았습니다")

        stats = data["stats"]
        self.stats_group.set_visible(enabled)
        if enabled:
            self.traffic_row.set_subtitle(
                f"↑ {stats['bytes_up'] / 1048576:.1f} MB   ↓ {stats['bytes_down'] / 1048576:.1f} MB"
            )
            self.conn_row.set_subtitle(
                f"활성 {stats['active']} · 누적 {stats['total']} · 실패 {stats['failed']}"
            )
            self.dns_row.set_subtitle(
                f"{stats['dns_queries']}건 (실패 {stats['dns_failed']})"
            )

        self._refresh_running()

    def _refresh_running(self) -> None:
        child = self.running_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.running_box.remove(child)
            child = nxt

        pids = appsel.routed_pids() if appsel.slice_exists() else []
        names: dict[str, list[int]] = {}
        for pid in pids:
            name = appsel.process_name(pid)
            if name in ("sleep", "(sd-pam)"):
                continue
            names.setdefault(name, []).append(pid)

        if not names:
            row = Adw.ActionRow(title="아직 없음", subtitle="위에서 앱을 실행하세요")
            row.set_sensitive(False)
            self.running_box.append(row)
            return

        for name, group in sorted(names.items()):
            row = Adw.ActionRow(
                title=name,
                subtitle=f"프로세스 {len(group)}개" if len(group) > 1 else f"PID {group[0]}",
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("application-x-executable-symbolic"))
            self.running_box.append(row)

    def _about(self) -> None:
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="USBTether",
            application_icon="network-cellular-symbolic",
            version=__version__,
            comments="USB로 연결한 폰의 모바일 데이터를 노트북 인터넷 회선으로 사용합니다.",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present()


class USBTetherApp(Adw.Application):
    """창을 닫아도 작업 표시줄 아이콘으로 남는 앱.

    아이콘은 GTK3 전용인 AppIndicator 를 쓰므로 별도 프로세스(usbtether-tray)가
    맡는다. 터널 자체는 백그라운드 데몬이 유지하므로 창이 떠 있든 말든,
    이 앱이 꺼져 있든 말든 연결은 그대로다.
    """

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:
        # 이미 떠 있는 인스턴스면 GApplication 이 여기로 보내 준다.
        # 숨어 있던 창이 그대로 다시 올라온다.
        window = self.props.active_window
        if window is None:
            tray.ensure_running()
            window = MainWindow(self)
        window.set_visible(True)
        window.present()


def main(argv: list[str] | None = None) -> int:
    return USBTetherApp().run(argv or [])
