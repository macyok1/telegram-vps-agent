import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform != 'win32':
    from server_console import ConsoleService, ConsoleStore
    from console_jobs import CommandJobs, owner_key


@unittest.skipIf(sys.platform == 'win32', 'Linux process and peer-credential integration')
class ConsoleSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConsoleStore(Path(self.tmp.name) / 'console.sqlite3')
        self.sent = []
        def send(data):
            self.sent.append(data)
            return 41
        self.service = ConsoleService(self.store, 'test-password', 'not-a-real-token', 42, 1001, send)
        self.service.jobs.cwd = self.tmp.name
        self.browser = 'a' * 43

    def tearDown(self):
        with self.service.jobs.lock:
            for item in self.service.jobs.running.values():
                item['cancel'].set()
        deadline = time.time() + 3
        while self.service.jobs.running and time.time() < deadline:
            time.sleep(.05)
        self.tmp.cleanup()

    def request(self):
        return self.service.dispatch('/request', {'browser':self.browser,'password':'test-password'}, 1001)

    def approve(self):
        self.request()
        data = self.sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        nonce = data.split(':', 2)[2]
        payload = {'action':'approve','nonce':nonce,'actor_id':42,'chat_id':42,'message_id':41}
        return self.service.dispatch('/decision', payload, 0), payload

    def test_locked_and_peer_boundary(self):
        with self.assertRaises(PermissionError):
            self.service.dispatch('/run', {'browser':self.browser,'command':'id'}, 1001)
        with self.assertRaises(PermissionError):
            self.service.dispatch('/status', {'browser':self.browser}, 2001)
        with self.assertRaises(PermissionError):
            self.service.dispatch('/decision', {}, 1001)

    def test_wrong_password_does_not_send_message(self):
        with self.assertRaises(PermissionError):
            self.service.dispatch('/request', {'browser':self.browser,'password':'wrong'}, 1001)
        self.assertEqual(self.sent, [])

    def test_browser_binding_replay_expiry_and_owner(self):
        _, payload = self.approve()
        self.assertTrue(self.store.allowed(self.browser))
        self.assertFalse(self.store.allowed('b' * 43))
        with self.assertRaises(PermissionError):
            self.service.dispatch('/decision', payload, 0)
        with self.assertRaises(PermissionError):
            self.service.dispatch('/decision', dict(payload, actor_id=43), 0)
        with self.store.connect() as db:
            db.execute('UPDATE grants SET expires=?', (time.time() - 1,))
        self.assertFalse(self.store.allowed(self.browser))

    def test_expired_request_and_wrong_message_rejected(self):
        self.request()
        nonce = self.sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'].split(':', 2)[2]
        payload = {'action':'approve','nonce':nonce,'actor_id':42,'chat_id':42,'message_id':44}
        with self.assertRaises(PermissionError):
            self.service.dispatch('/decision', payload, 0)
        with self.store.connect() as db:
            db.execute('UPDATE approvals SET expires=?', (time.time() - 1,))
        with self.assertRaises(PermissionError):
            self.service.dispatch('/decision', dict(payload, message_id=41), 0)

    def await_job(self, jid, seconds=8):
        deadline = time.time() + seconds
        while time.time() < deadline:
            row = self.service.jobs.get(self.browser, jid)
            if row['state'] != 'running':
                return row
            time.sleep(.05)
        self.fail('Command did not terminate')

    def test_output_exit_and_no_secret_environment(self):
        self.approve()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'must-not-be-inherited'}):
            jid = self.service.dispatch('/run', {'browser':self.browser,
                'command':"printf 'hello'; printf '%s' \"$TELEGRAM_BOT_TOKEN\"; printf 'problem' >&2; exit 7"}, 1001)['id']
            row = self.await_job(jid)
        self.assertEqual(row['stdout'], 'hello')
        self.assertEqual(row['stderr'], 'problem')
        self.assertEqual(row['exit_code'], 7)

    def test_cancel_kills_descendants_and_other_browser_cannot_cancel(self):
        self.approve()
        jid = self.service.dispatch('/run', {'browser':self.browser,'command':'sleep 30 & echo $!; wait'}, 1001)['id']
        time.sleep(.6)
        with self.assertRaises(KeyError):
            self.service.jobs.cancel('b' * 43, jid)
        self.service.jobs.cancel(self.browser, jid)
        row = self.await_job(jid)
        self.assertIn('Остановлено', row['reason'])
        pid = int(row['stdout'].strip())
        stat = Path(f'/proc/{pid}/stat')
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')

    def test_revoke_kills_job_and_blocks_next_job(self):
        _, payload = self.approve()
        jid = self.service.dispatch('/run', {'browser':self.browser,'command':'sleep 30'}, 1001)['id']
        self.service.dispatch('/decision', dict(payload, action='revoke'), 0)
        row = self.await_job(jid)
        self.assertIn('Доступ отозван', row['reason'])
        with self.assertRaises(PermissionError):
            self.service.dispatch('/run', {'browser':self.browser,'command':'true'}, 1001)

    def test_timeout_and_output_limit(self):
        self.approve()
        jid = self.service.dispatch('/run', {'browser':self.browser,'command':'sleep 30','timeout':5}, 1001)['id']
        self.assertIn('таймаут', self.await_job(jid)['reason'])
        jid = self.service.dispatch('/run', {'browser':self.browser,'command':'yes output'}, 1001)['id']
        row = self.await_job(jid)
        self.assertEqual(row['truncated'], 1)
        self.assertLessEqual(len(row['stdout'].encode()), 262144)

    def test_stored_state_permissions_and_restart(self):
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.approve()
        fresh = ConsoleStore(self.store.path)
        self.assertTrue(fresh.allowed(self.browser))


if __name__ == '__main__':
    unittest.main()
