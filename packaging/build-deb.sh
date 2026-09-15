#!/usr/bin/env bash
# .deb 패키지를 만든다. debhelper 없이 dpkg-deb 만 쓴다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(sed -n 's/^Version: //p' "$ROOT/packaging/debian/control")"
STAGE="$ROOT/build/usbtether_${VERSION}_all"
OUT="$ROOT/build/usbtether_${VERSION}_all.deb"

rm -rf "$STAGE"
mkdir -p \
    "$STAGE/DEBIAN" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/lib/python3/dist-packages/usbtether" \
    "$STAGE/usr/lib/systemd/system" \
    "$STAGE/usr/share/applications" \
    "$STAGE/etc/xdg/autostart" \
    "$STAGE/usr/share/polkit-1/actions" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" \
    "$STAGE/usr/share/doc/usbtether" \
    "$STAGE/etc/usbtether"

install -m 0644 "$ROOT"/src/usbtether/*.py "$STAGE/usr/lib/python3/dist-packages/usbtether/"
install -m 0755 "$ROOT"/bin/usbtether "$STAGE/usr/bin/usbtether"
install -m 0755 "$ROOT"/bin/usbtether-gui "$STAGE/usr/bin/usbtether-gui"
install -m 0755 "$ROOT"/bin/usbtetherd "$STAGE/usr/bin/usbtetherd"
install -m 0755 "$ROOT"/bin/usbtether-tray "$STAGE/usr/bin/usbtether-tray"
install -m 0644 "$ROOT/data/usbtether.service" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$ROOT/data/usbtether.desktop" "$STAGE/usr/share/applications/"
install -m 0644 "$ROOT/data/usbtether-tray.desktop" "$STAGE/etc/xdg/autostart/"
install -m 0644 "$ROOT/data/org.usbtether.policy" "$STAGE/usr/share/polkit-1/actions/"
install -m 0644 "$ROOT/data/icons/usbtether.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$ROOT/data/icons/usbtether-off.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/usbtether/"
install -m 0644 "$ROOT/data/config.json" "$STAGE/etc/usbtether/config.json"

install -m 0644 "$ROOT/packaging/debian/control" "$STAGE/DEBIAN/control"
for script in postinst prerm postrm; do
    install -m 0755 "$ROOT/packaging/debian/$script" "$STAGE/DEBIAN/$script"
done

# 설정 파일은 제거 시 보존한다
echo "/etc/usbtether/config.json" > "$STAGE/DEBIAN/conffiles"

find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"

echo
echo "빌드 완료: $OUT"
dpkg-deb --info "$OUT" | sed -n '1,12p'
