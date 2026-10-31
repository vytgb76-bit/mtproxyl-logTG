#!/usr/bin/env python3
"""Offline unit tests; no Telegram/network calls."""
import datetime as dt
import gzip
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('logship', str(Path(__file__).with_name('logship.py')))
logship = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logship)


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.stdout = io.BytesIO(b'2026-10-31T01:01:02.345678+03:00 telemt: MTProto handshake successful\n')
    def wait(self, timeout=None):
        return 0
    def kill(self):
        return None


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        logship.BASE = base
        logship.PENDING = base / 'pending'
        logship.PENDING.mkdir()
        logship.CURSOR = base / 'cursor.json'
        logship.LOCK = base / 'lock'
        self.cfg = {'server_name': 'VPS_NL', 'unit': 'mtproxyl-telemt', 'chat_id': '123', 'bot_token': '123:aaa'}

    def test_journal_export_and_source_label(self):
        with patch.object(logship.subprocess, 'Popen', FakeProcess):
            file = logship.archive_day(self.cfg, dt.date(2026,10,31))
        with gzip.open(file, 'rt') as f:
            content = f.read()
        self.assertIn('VPS_NL', content)
        self.assertIn('handshake successful', content)
        self.assertEqual(file.name, 'VPS_NL_2026-10-31.log.gz')

    def test_docker_export_command_and_stderr(self):
        cfg = {'server_name': 'VPS_DE', 'source_kind': 'docker',
               'source_target': 'mtproxyl', 'chat_id': '123', 'bot_token': '123:aaa'}
        calls = []
        class DockerProcess:
            def __init__(self, cmd, **kwargs):
                calls.append((cmd, kwargs))
                self.stdout = io.BytesIO(b'2026-10-31T01:01:02+03:00 MTProto handshake successful\n')
            def wait(self, timeout=None):
                return 0
            def kill(self):
                return None
        with patch.object(logship.subprocess, 'Popen', DockerProcess):
            file = logship.archive_day(cfg, dt.date(2026, 10, 31))
        with gzip.open(file, 'rt') as fh:
            content = fh.read()
        self.assertIn('Source: docker / mtproxyl', content)
        self.assertIn('handshake successful', content)
        self.assertTrue(calls[0][0][0].endswith('/docker'))
        self.assertIn('--since', calls[0][0])
        self.assertIn('--until', calls[0][0])
        self.assertEqual(calls[0][1]['stderr'], logship.subprocess.STDOUT)

    def test_docker_error_keeps_no_partial_archive(self):
        cfg = {'server_name': 'VPS_DE', 'source_kind': 'docker',
               'source_target': 'mtproxyl', 'chat_id': '123', 'bot_token': '123:aaa'}
        class FailedProcess:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(b'Error response from daemon: logging driver unsupported\n')
            def wait(self, timeout=None):
                return 1
            def kill(self):
                return None
        with patch.object(logship.subprocess, 'Popen', FailedProcess):
            with self.assertRaisesRegex(RuntimeError, 'Docker|docker'):
                logship.archive_day(cfg, dt.date(2026, 10, 31))
        self.assertEqual(list(logship.PENDING.iterdir()), [])

    def test_legacy_binary_config(self):
        self.assertEqual(logship.log_source(self.cfg), ('service', 'mtproxyl-telemt'))

    def test_pending_is_retained_and_resumed_after_failed_send(self):
        file = logship.PENDING / 'VPS_NL_2026-10-31.log.gz'
        file.write_bytes(b'abcdefghijklmnopqrstuvwxyz')
        calls = []
        def fake_upload(path, offset, count, filename, caption, cfg):
            calls.append((offset, count))
            if len(calls) == 2:
                raise RuntimeError('network down')
        with patch.object(logship, 'PART_LIMIT', 10), patch.object(logship, 'upload', side_effect=fake_upload):
            ok = logship.deliver_pending(self.cfg)
        self.assertFalse(ok)
        self.assertTrue(file.exists())
        progress = logship.load_json(file.with_name(file.name+'.progress.json'))
        self.assertEqual(progress['next_part'], 1)
        with patch.object(logship, 'PART_LIMIT', 10), patch.object(logship, 'upload', side_effect=fake_upload):
            ok = logship.deliver_pending(self.cfg)
        self.assertTrue(ok)
        self.assertFalse(file.exists())
        self.assertEqual(calls, [(0,10), (10,10), (10,10), (20,6)])

    def test_no_data_loss_if_archive_exists_after_interruption(self):
        day = dt.date.today() - dt.timedelta(days=1)
        logship.atomic_json(logship.CURSOR, {'next_date': day.isoformat()})
        name = f'VPS_NL_{day.isoformat()}.log.gz'
        file = logship.PENDING / name
        file.write_bytes(b'existing finished archive')
        with patch.object(logship.subprocess, 'Popen', side_effect=AssertionError('Should not re-export')):
            logship.collect(self.cfg)
        self.assertEqual(file.read_bytes(), b'existing finished archive')
        self.assertEqual(logship.load_json(logship.CURSOR)['next_date'], dt.date.today().isoformat())


if __name__ == '__main__':
    unittest.main(verbosity=2)
