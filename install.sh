#!/usr/bin/env bash
# Installs the logger only. Does not restart or reconfigure MTProxyL/Telemt.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Запустите: sudo bash install.sh' >&2; exit 1; fi
for bin in python3 systemctl; do
  command -v "$bin" >/dev/null || { echo "Нет команды $bin" >&2; exit 1; }
done
[[ -f /opt/mtproxyl/settings.conf ]] || { echo 'Не найдена установка MTProxyL (/opt/mtproxyl/settings.conf)' >&2; exit 1; }
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ -f "$DIR/logship.py" ]] || { echo 'Рядом с install.sh должен находиться logship.py' >&2; exit 1; }
python3 -m py_compile "$DIR/logship.py"

# MTProxyL CLI is the authoritative way to find its log source.
# Use a read-only settings.conf fallback only if CLI is unavailable.
if ! SOURCE_INFO=$(python3 - <<'PY_DETECT'
import json, re, subprocess, sys
from pathlib import Path

valid = re.compile(r'[A-Za-z0-9_.@-]{1,100}\Z')
data = None
try:
    result = subprocess.run(['mtproxyl', 'mode', '--json'], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    for line in result.stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and 'log_kind' in obj:
            data = obj
            break
except (OSError, subprocess.TimeoutExpired):
    pass

if data is not None:
    kind = data.get('log_kind', '')
    target = data.get('log_target', '')
    if data.get('running') is not True:
        print('Ошибка: MTProxyL сообщает running=false. Сначала проверьте сам прокси.', file=sys.stderr)
        sys.exit(2)
    if kind not in ('docker', 'service') or not isinstance(target, str) or not valid.fullmatch(target):
        print('Ошибка: MTProxyL сообщил неподдерживаемый источник логов:', kind, target, file=sys.stderr)
        sys.exit(2)
else:
    print('Предупреждение: mode --json недоступен. Используется settings.conf.', file=sys.stderr)
    settings = {}
    for line in Path('/opt/mtproxyl/settings.conf').read_text(encoding='utf-8', errors='replace').splitlines():
        match = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$', line.strip())
        if match:
            settings[match.group(1)] = match.group(2).strip().strip('"\'')
    if settings.get('MTPROXYL_MODE', 'manager') != 'manager':
        print('Ошибка: автоматическое определение в Reanimator недоступно без mode --json.', file=sys.stderr)
        sys.exit(2)
    backend = settings.get('ENGINE_BACKEND', '')
    if backend == 'docker':
        kind, target = 'docker', 'mtproxyl'
    elif backend == 'binary':
        kind = 'service'
        target = settings.get('ENGINE_SERVICE_NAME', '') if settings.get('ENGINE_SERVICE_NAMING') == 'random' else 'mtproxyl-telemt'
    else:
        print('Ошибка: неизвестный ENGINE_BACKEND:', backend, file=sys.stderr)
        sys.exit(2)
    if not target or not valid.fullmatch(target):
        print('Ошибка: нельзя определить имя службы/контейнера.', file=sys.stderr)
        sys.exit(2)

print(kind + '|' + target)
PY_DETECT
); then
  echo 'Установка остановлена. Получить сведения можно командой: sudo mtproxyl mode --json' >&2
  exit 1
fi
IFS='|' read -r SOURCE_KIND SOURCE_TARGET <<< "$SOURCE_INFO"

if [[ "$SOURCE_KIND" == docker ]]; then
  command -v docker >/dev/null || { echo 'Docker не найден в PATH.' >&2; exit 1; }
  CONTAINER_RUNNING=$(docker inspect --format '{{.State.Running}}' "$SOURCE_TARGET" 2>/dev/null) || {
    echo "Не удалось найти Docker-контейнер: $SOURCE_TARGET" >&2; exit 1;
  }
  [[ "$CONTAINER_RUNNING" == true ]] || { echo "Контейнер $SOURCE_TARGET не работает" >&2; exit 1; }
  # Fail early for logging drivers that do not support docker logs.
  docker logs --tail 1 "$SOURCE_TARGET" >/dev/null 2>&1 || {
    echo "Docker не смог прочитать логи контейнера $SOURCE_TARGET" >&2
    echo 'Проверьте: sudo docker logs --tail 10 mtproxyl' >&2
    exit 1
  }
  echo "Источник обнаружен: Docker-контейнер $SOURCE_TARGET"
else
  command -v journalctl >/dev/null || { echo 'journalctl не найден в PATH.' >&2; exit 1; }
  systemctl is-active --quiet "${SOURCE_TARGET}.service" || {
    echo "Служба ${SOURCE_TARGET}.service неактивна или не найдена" >&2; exit 1;
  }
  echo "Источник обнаружен: systemd-служба ${SOURCE_TARGET}.service"
fi

if [[ -f /etc/mtproxyl-logship.json ]]; then
  echo 'Настройки отправщика уже существуют: /etc/mtproxyl-logship.json' >&2
  echo 'Повторная установка поверх существующей не выполняется во избежание потери настроек.' >&2
  exit 1
fi

printf 'Имя ЭТОГО VPS (латиница, цифры, _ и -, например VPS_DE): '
IFS= read -r SERVER
[[ "$SERVER" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo 'Неверное имя VPS' >&2; exit 1; }
printf 'Токен Telegram-бота (общий для всех VPS): '
IFS= read -rs BOT_TOKEN
printf '\n'
[[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo 'Неверный формат токена' >&2; exit 1; }
printf 'Числовой chat_id (общий для всех VPS): '
IFS= read -r CHAT_ID
[[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] || { echo 'Неверный формат chat_id' >&2; exit 1; }

umask 077
install -d -m 0700 /var/lib/mtproxyl-logship/pending /opt/mtproxyl-logship
install -m 0700 "$DIR/logship.py" /opt/mtproxyl-logship/logship.py
export MTPROXY_SHIP_SERVER="$SERVER" MTPROXY_SHIP_KIND="$SOURCE_KIND" MTPROXY_SHIP_TARGET="$SOURCE_TARGET" MTPROXY_SHIP_TOKEN="$BOT_TOKEN" MTPROXY_SHIP_CHAT="$CHAT_ID"
python3 - <<'PY_CONFIG'
import datetime as dt, json, os
config = {
    'server_name': os.environ['MTPROXY_SHIP_SERVER'],
    'source_kind': os.environ['MTPROXY_SHIP_KIND'],
    'source_target': os.environ['MTPROXY_SHIP_TARGET'],
    'bot_token': os.environ['MTPROXY_SHIP_TOKEN'],
    'chat_id': os.environ['MTPROXY_SHIP_CHAT'],
}
for filename, data in (
    ('/etc/mtproxyl-logship.json', config),
    ('/var/lib/mtproxyl-logship/cursor.json', {'next_date': dt.date.today().isoformat()}),
):
    with open(filename, 'x', encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False)
        out.write('\n')
    os.chmod(filename, 0o600)
PY_CONFIG
unset BOT_TOKEN CHAT_ID MTPROXY_SHIP_TOKEN MTPROXY_SHIP_CHAT

cat > /etc/systemd/system/mtproxyl-logship.service <<'EOF_SERVICE'
[Unit]
Description=MTProxyL daily logs to Telegram (Docker or systemd)
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/usr/bin/python3 /opt/mtproxyl-logship/logship.py run
TimeoutStartSec=1200
NoNewPrivileges=true
EOF_SERVICE
cat > /etc/systemd/system/mtproxyl-logship.timer <<'EOF_TIMER'
[Unit]
Description=MTProxyL log shipper at midnight and hourly retry
[Timer]
OnCalendar=hourly
AccuracySec=1s
Persistent=true
Unit=mtproxyl-logship.service
[Install]
WantedBy=timers.target
EOF_TIMER
chmod 644 /etc/systemd/system/mtproxyl-logship.{service,timer}
systemctl daemon-reload
systemctl enable --now mtproxyl-logship.timer
printf '\nУстановка завершена. MTProxyL и Telemt не перезапускались.\n'
printf 'Источник: %s / %s\n' "$SOURCE_KIND" "$SOURCE_TARGET"
printf 'Пример архива: %s_%s.log.gz\n' "$SERVER" "$(date +%F)"
printf '\nТест Telegram: sudo python3 /opt/mtproxyl-logship/logship.py test\n'
printf 'Статус: sudo python3 /opt/mtproxyl-logship/logship.py status\n'
printf 'Для подробных событий: sudo mtproxyl tune set log_level verbose\n'
