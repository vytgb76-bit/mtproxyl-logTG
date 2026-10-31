#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Запустите: sudo bash install.sh' >&2; exit 1; fi
for bin in python3 journalctl systemctl; do
  command -v "$bin" >/dev/null || { echo "Нет команды $bin" >&2; exit 1; }
done
[[ -f /opt/mtproxyl/settings.conf ]] || { echo 'Не найдена установка MTProxyL (/opt/mtproxyl/settings.conf)' >&2; exit 1; }
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ -f "$DIR/logship.py" ]] || { echo 'Рядом с установщиком нет logship.py' >&2; exit 1; }
python3 -m py_compile "$DIR/logship.py"

# MTProxyL exposes the exact source it uses for its "Streaming logs" menu.
CANDIDATE=$(python3 - <<'PY'
import json, re, subprocess
try:
    p = subprocess.run(['mtproxyl','mode','--json'], capture_output=True, text=True, timeout=20)
    for line in reversed(p.stdout.splitlines()):
        try: data=json.loads(line)
        except ValueError: continue
        if data.get('engine') == 'binary' and data.get('log_kind') == 'service':
            name=data.get('log_target', '')
            if re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', name):
                print(name)
                raise SystemExit(0)
except (OSError, subprocess.TimeoutExpired): pass
# Older installations: settings.conf (do not source/execute its content).
settings={}
with open('/opt/mtproxyl/settings.conf', encoding='utf-8', errors='replace') as f:
    for line in f:
        m=re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line.strip())
        if m: settings[m.group(1)] = m.group(2).strip().strip('"\'')
if settings.get('ENGINE_BACKEND') != 'binary':
    raise SystemExit(2)
name='mtproxyl-telemt'
if settings.get('ENGINE_SERVICE_NAMING') == 'random':
    name=settings.get('ENGINE_SERVICE_NAME', '')
if re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', name): print(name)
PY
) || { echo 'Не удалось подтвердить бинарный режим. Установка прервана.' >&2; exit 1; }
if [[ -z "$CANDIDATE" ]] || ! systemctl cat "${CANDIDATE}.service" >/dev/null 2>&1; then
  echo "Не найдена служба MTProxyL ${CANDIDATE}.service" >&2
  echo 'Проверьте: sudo mtproxyl mode --json' >&2
  exit 1
fi
if ! systemctl is-active --quiet "${CANDIDATE}.service"; then
  echo "Служба ${CANDIDATE}.service сейчас не работает. Сначала исправьте прокси." >&2; exit 1
fi
if [[ -f /etc/mtproxyl-logship.json ]]; then
  echo 'Прежняя конфигурация отправки уже существует; установщик не перезаписывает её.' >&2
  echo 'Для смены имени сервера и параметров отредактируйте /etc/mtproxyl-logship.json' >&2
  exit 1
fi
printf '\nОбнаружен бинарный Telemt: %s.service\n' "$CANDIDATE"
printf 'Название ЭТОГО VPS (латиница/цифры/дефис, например VPS_NL): '
IFS= read -r SERVER
[[ $SERVER =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo 'Некорректное имя VPS' >&2; exit 1; }
printf 'Токен Telegram-бота (можно один и тот же на разных VPS): '
IFS= read -rs BOT_TOKEN
printf '\n'
[[ $BOT_TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo 'Неверный формат токена' >&2; exit 1; }
printf 'Числовой chat_id (для всех VPS одинаковый): '
IFS= read -r CHAT_ID
[[ $CHAT_ID =~ ^-?[0-9]+$ ]] || { echo 'Неверный chat_id' >&2; exit 1; }

umask 077
install -d -m 0700 /var/lib/mtproxyl-logship/pending /opt/mtproxyl-logship
install -m 0700 "$DIR/logship.py" /opt/mtproxyl-logship/logship.py
export MTPROXY_SHIP_SERVER="$SERVER" MTPROXY_SHIP_UNIT="$CANDIDATE" MTPROXY_SHIP_TOKEN="$BOT_TOKEN" MTPROXY_SHIP_CHAT="$CHAT_ID"
python3 - <<'PY'
import datetime as dt, json, os
from pathlib import Path
cfg={"server_name":os.environ['MTPROXY_SHIP_SERVER'],
     "unit":os.environ['MTPROXY_SHIP_UNIT'],
     "bot_token":os.environ['MTPROXY_SHIP_TOKEN'],
     "chat_id":os.environ['MTPROXY_SHIP_CHAT']}
for path,obj in [('/etc/mtproxyl-logship.json',cfg),
                 ('/var/lib/mtproxyl-logship/cursor.json',{'next_date':dt.date.today().isoformat()})]:
    with open(path,'x',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False)
        f.write('\n')
    os.chmod(path,0o600)
PY
unset BOT_TOKEN CHAT_ID MTPROXY_SHIP_TOKEN MTPROXY_SHIP_CHAT
cat > /etc/systemd/system/mtproxyl-logship.service <<'EOF'
[Unit]
Description=MTProxyL daily logs to Telegram (journal export)
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/usr/bin/python3 /opt/mtproxyl-logship/logship.py run
TimeoutStartSec=1200
NoNewPrivileges=true
EOF
cat > /etc/systemd/system/mtproxyl-logship.timer <<'EOF'
[Unit]
Description=MTProxyL log shipper at midnight and hourly retry
[Timer]
OnCalendar=hourly
AccuracySec=1s
Persistent=true
Unit=mtproxyl-logship.service
[Install]
WantedBy=timers.target
EOF
chmod 644 /etc/systemd/system/mtproxyl-logship.{service,timer}
systemctl daemon-reload
systemctl enable --now mtproxyl-logship.timer
printf '\nУстановлено. Telemt не перезапускался, его настройки не изменялись.\n'
printf 'Пример будущего имени файла: %s_%s.log.gz\n' "$SERVER" "$(date +%F)"
printf '\nПроверьте доставку тестового файла:\n'
printf '  sudo python3 /opt/mtproxyl-logship/logship.py test\n'
printf '\nСтатус:\n'
printf '  sudo python3 /opt/mtproxyl-logship/logship.py status\n'
printf '  systemctl list-timers mtproxyl-logship.timer\n'
printf '\nВключение подробных записей (по желанию): sudo mtproxyl tune set log_level verbose\n'
