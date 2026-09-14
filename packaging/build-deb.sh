#!/usr/bin/env bash
# .deb 패키지를 만든다. debhelper 없이 dpkg-deb 만 쓴다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(sed -n 's/^Version: //p' "$ROOT/packaging/debian/control")"
STAGE="$ROOT/build/phone-socks_${VERSION}_all"
OUT="$ROOT/build/phone-socks_${VERSION}_all.deb"

rm -rf "$STAGE"
mkdir -p \
    "$STAGE/DEBIAN" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/lib/python3/dist-packages/phonesocks" \
    "$STAGE/usr/lib/systemd/system" \
    "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/polkit-1/actions" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" \
    "$STAGE/usr/share/doc/phone-socks" \
    "$STAGE/etc/phone-socks"

install -m 0644 "$ROOT"/src/phonesocks/*.py "$STAGE/usr/lib/python3/dist-packages/phonesocks/"
install -m 0755 "$ROOT"/bin/phone-socks "$STAGE/usr/bin/phone-socks"
install -m 0755 "$ROOT"/bin/phone-socks-gui "$STAGE/usr/bin/phone-socks-gui"
install -m 0755 "$ROOT"/bin/phone-socksd "$STAGE/usr/bin/phone-socksd"
install -m 0644 "$ROOT/data/phone-socks.service" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$ROOT/data/phone-socks.desktop" "$STAGE/usr/share/applications/"
install -m 0644 "$ROOT/data/org.phonesocks.policy" "$STAGE/usr/share/polkit-1/actions/"
install -m 0644 "$ROOT/data/icons/phone-socks.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/phone-socks/"
install -m 0644 "$ROOT/data/config.json" "$STAGE/etc/phone-socks/config.json"

install -m 0644 "$ROOT/packaging/debian/control" "$STAGE/DEBIAN/control"
for script in postinst prerm postrm; do
    install -m 0755 "$ROOT/packaging/debian/$script" "$STAGE/DEBIAN/$script"
done

# 설정 파일은 제거 시 보존한다
echo "/etc/phone-socks/config.json" > "$STAGE/DEBIAN/conffiles"

find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"

echo
echo "빌드 완료: $OUT"
dpkg-deb --info "$OUT" | sed -n '1,12p'
