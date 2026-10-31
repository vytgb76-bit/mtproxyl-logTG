#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Запустите с sudo' >&2; exit 1; fi
systemctl disable --now mtproxyl-logship.timer 2>/dev/null || true
systemctl stop mtproxyl-logship.service 2>/dev/null || true
rm -f /etc/systemd/system/mtproxyl-logship.service /etc/systemd/system/mtproxyl-logship.timer
systemctl daemon-reload
printf 'Службы отправки удалены. Сохранены /etc/mtproxyl-logship.json и /var/lib/mtproxyl-logship (включая неотправленные архивы).\n'
printf 'Для удаления этих данных вручную: sudo rm -rf /var/lib/mtproxyl-logship; sudo rm -f /etc/mtproxyl-logship.json\n'
printf 'MTProxyL/Telemt не изменялись.\n'
