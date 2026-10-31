#!/usr/bin/env python3
"""MTProxyL journal -> one Telegram bot, per-host daily archives.

Requires Python >= 3.8 and either systemd journalctl or Docker CLI. Does not modify proxy service.
"""
import datetime as dt
import fcntl
import gzip
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

CONF = Path('/etc/mtproxyl-logship.json')
BASE = Path('/var/lib/mtproxyl-logship')
PENDING = BASE / 'pending'
CURSOR = BASE / 'cursor.json'
LOCK = BASE / 'lock'
PART_LIMIT = 45 * 1024 * 1024


def load_json(path, default=None):
    try:
        with path.open('r', encoding='utf-8') as inp:
            return json.load(inp)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic_json(path, obj):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(obj, out, ensure_ascii=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def settings():
    cfg = load_json(CONF)
    name = cfg.get('server_name', '')
    kind, target = log_source(cfg)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name):
        raise ValueError('Invalid server_name')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', target):
        raise ValueError('Invalid log source name')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', cfg.get('bot_token', '')):
        raise ValueError('Invalid bot token')
    if not re.fullmatch(r'-?[0-9]+', str(cfg.get('chat_id', ''))):
        raise ValueError('Invalid chat_id')
    return cfg



def log_source(cfg):
    """Support older configs from binary-only installs without changing them."""
    if 'source_kind' in cfg or 'source_target' in cfg:
        kind = cfg.get('source_kind')
        target = cfg.get('source_target')
    else:
        kind, target = 'service', cfg.get('unit')
    if kind not in ('service', 'docker'):
        raise ValueError('source_kind must be service or docker')
    if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', target):
        raise ValueError('Invalid source_target/unit')
    return kind, target


def export_command(cfg, start, end):
    kind, target = log_source(cfg)
    if kind == 'service':
        return ['/usr/bin/journalctl', '--quiet', '--no-pager',
                '-u', target, '-o', 'short-iso-precise',
                '--since', '@' + str(start),
                '--until', f'@{end - 0.000001:.6f}']
    # UTC absolute timestamps avoid ambiguity on DST transitions.
    fmt = lambda stamp: dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(timespec='seconds')
    return [shutil.which('docker') or '/usr/bin/docker', 'logs', '--timestamps', '--since', fmt(start),
            '--until', fmt(end), target]

def next_midnight(date):
    # Uses server's local timezone, including DST changes.
    return int(time.mktime((date.year, date.month, date.day, 0, 0, 0, 0, 0, -1)))


def archive_day(cfg, day):
    name = cfg['server_name']
    filename = f'{name}_{day.isoformat()}.log.gz'
    target = PENDING / filename
    if target.exists():
        return target
    start = next_midnight(day)
    end = next_midnight(day + dt.timedelta(days=1))
    kind, source_target = log_source(cfg)
    cmd = export_command(cfg, start, end)
    fd, tmp = tempfile.mkstemp(prefix='.archive-', dir=PENDING)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(filename=filename[:-3], mode='wb', fileobj=raw, mtime=0) as out:
                out.write((f'# MTProxyL daily log\n# Server: {name}\n'
                           f'# Source: {kind} / {source_target}\n# Date: {day}\n'
                           f'# Local time: {time.strftime("%Z %z")}\n\n').encode())
                with tempfile.TemporaryFile(mode='w+b') as errors:
                    # Docker emits container stdout/stderr on two CLI streams.
                    # Merge them; do not silently omit failed handshakes/errors.
                    capture_docker = (kind == 'docker')
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=(subprocess.STDOUT if capture_docker else errors))
                    preview = bytearray()
                    try:
                        assert proc.stdout is not None
                        while True:
                            block = proc.stdout.read(256 * 1024)
                            if not block:
                                break
                            out.write(block)
                            if capture_docker and len(preview) < 512:
                                preview.extend(block[:512-len(preview)])
                        code = proc.wait(timeout=120)
                    except BaseException:
                        proc.kill()
                        proc.wait()
                        raise
                    if code != 0:
                        errors.seek(0)
                        detail = errors.read(512).decode('utf-8', errors='replace').strip()
                        if capture_docker:
                            detail = bytes(preview).decode('utf-8', errors='replace').strip()
                        raise RuntimeError(f'{kind} log export failed ({code}): {detail}')
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, target)
        print(f'Archived {filename}: {target.stat().st_size} bytes', flush=True)
        return target
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def collect(cfg):
    cursor = load_json(CURSOR)
    day = dt.date.fromisoformat(cursor['next_date'])
    today = dt.date.today()
    if day > today:
        raise RuntimeError(f'Clock/date appears to have moved backwards: cursor={day}, today={today}')
    count = 0
    while day < today and count < 31:
        archive_day(cfg, day)
        day += dt.timedelta(days=1)
        atomic_json(CURSOR, {'next_date': day.isoformat()})
        count += 1
    if day < today:
        print('More backlog dates remain; they will be collected next hour.', flush=True)


def form_field(boundary, key, value):
    return (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode('utf-8')


def upload(path, offset, count, filename, caption, cfg):
    boundary = 'mtpl' + secrets.token_hex(16)
    header = (form_field(boundary, 'chat_id', str(cfg['chat_id'])) +
              form_field(boundary, 'caption', caption) +
              f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode('utf-8'))
    footer = f'\r\n--{boundary}--\r\n'.encode('ascii')
    con = http.client.HTTPSConnection('api.telegram.org', timeout=240)
    try:
        con.putrequest('POST', '/bot' + cfg['bot_token'] + '/sendDocument')
        con.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
        con.putheader('Content-Length', str(len(header) + count + len(footer)))
        con.endheaders()
        con.send(header)
        with path.open('rb') as inp:
            inp.seek(offset)
            remaining = count
            while remaining:
                data = inp.read(min(256 * 1024, remaining))
                if not data:
                    raise IOError('Unexpected EOF when reading archive')
                con.send(data)
                remaining -= len(data)
        con.send(footer)
        reply = con.getresponse()
        body = reply.read(16384)
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise RuntimeError(f'Telegram returned non-JSON HTTP {reply.status}')
        if reply.status != 200 or result.get('ok') is not True:
            raise RuntimeError('Telegram API: HTTP ' + str(reply.status) + ', ' + str(result.get('description', 'error'))[:240])
    finally:
        con.close()


def deliver_pending(cfg):
    failed = False
    for path in sorted(PENDING.glob(f'{cfg["server_name"]}_*.log.gz')):
        total_bytes = path.stat().st_size
        total_parts = max(1, math.ceil(total_bytes / PART_LIMIT))
        progress = path.with_name(path.name + '.progress.json')
        saved = load_json(progress, {'next_part': 0, 'file_size': total_bytes})
        if saved.get('file_size') != total_bytes:
            raise RuntimeError(f'Pending archive changed size: {path.name}')
        i = saved['next_part']
        try:
            while i < total_parts:
                offset = i * PART_LIMIT
                count = min(PART_LIMIT, total_bytes - offset)
                fn = path.name if total_parts == 1 else f'{path.name}.part{i + 1:03d}-of-{total_parts:03d}'
                caption = f'MTProxyL / {cfg["server_name"]} / {path.name[len(cfg["server_name"])+1:len(cfg["server_name"])+11]}'
                if total_parts > 1:
                    caption += f' / part {i + 1}/{total_parts}, join in order'
                upload(path, offset, count, fn, caption, cfg)
                i += 1
                atomic_json(progress, {'next_part': i, 'file_size': total_bytes})
                print('Delivered:', fn, flush=True)
            path.unlink()
            progress.unlink(missing_ok=True)
            print('Deleted confirmed archive:', path.name, flush=True)
        except Exception as exc:
            print(f'ERROR: {path.name}: {exc}; will retry hourly', file=sys.stderr, flush=True)
            failed = True
    return not failed


def do_test(cfg):
    kind, target = log_source(cfg)
    with tempfile.NamedTemporaryFile(mode='wb', dir=BASE, prefix='.test-', delete=False) as tmp:
        path = Path(tmp.name)
        tmp.write((f'Test connection to Telegram bot\nServer: {cfg["server_name"]}\nSource: {kind} / {target}\n').encode())
    try:
        upload(path, 0, path.stat().st_size,
               f'{cfg["server_name"]}_TEST.txt', f'Проверка логов: {cfg["server_name"]}', cfg)
        print('TEST OK: Telegram confirmed the document')
    finally:
        path.unlink(missing_ok=True)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('run', 'test', 'status'):
        print('Usage: logship.py run|test|status', file=sys.stderr)
        return 2
    cfg = settings()
    if sys.argv[1] == 'status':
        print('Server:', cfg['server_name'])
        kind, target = log_source(cfg)
        print('Source:', 'journalctl -u' if kind == 'service' else 'docker logs', target)
        print('Today (VPS local time):', dt.date.today())
        print('Next date to archive:', load_json(CURSOR).get('next_date'))
        print('Unsent archives:', len(list(PENDING.glob('*.log.gz'))))
        return 0
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    PENDING.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open('a+b') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if sys.argv[1] == 'test':
            do_test(cfg)
            return 0
        collect_error = None
        try:
            collect(cfg)
        except Exception as exc:
            collect_error = exc
            print('ERROR collecting logs:', str(exc), file=sys.stderr)
        sent = deliver_pending(cfg)
        return 0 if sent and collect_error is None else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
