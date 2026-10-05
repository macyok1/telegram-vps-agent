from pathlib import Path
import tempfile
import unittest
from command_policy import needs_approval
from server_console import ConsoleStore, ConsoleService

class ToolApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ConsoleService(ConsoleStore(Path(self.tmp.name)/'state.db'), 'password', 'token', 42, 1000, send=lambda _:17)
        self.browser = 'telegram-tool:' + 'a'*32

    def tearDown(self):
        self.tmp.cleanup()

    def test_conservative_read_only_policy(self):
        for text in ['whoami', 'date -Is', 'hostname', 'ls -la /root', 'systemctl status ssh --no-pager']:
            self.assertFalse(needs_approval(text), text)
        for text in ['rm -rf /tmp/a', 'systemctl restart ssh', 'hostname changed', 'date 010100002026',
                     'date -s now', 'journalctl --vacuum-time=1d', 'ls; rm -rf /tmp/a',
                     'echo $(touch /tmp/a)', 'python3 -c "print(1)"', '/bin/ls /tmp', 'bash -c whoami']:
            self.assertTrue(needs_approval(text), text)

    def decision(self, nonce, **extra):
        return dict(action='approve', nonce=nonce, actor_id=42, chat_id=42, message_id=17, **extra)

    def test_exact_command_owner_and_replay(self):
        ticket = self.service.dispatch('/tool/request', {'browser':self.browser, 'command':'touch /tmp/approved'}, 0)
        nonce = ticket['nonce']
        data = self.decision(nonce)
        with self.assertRaises(PermissionError): self.service.dispatch('/tool/decision', data, 1000)
        wrong = dict(data, actor_id=43)
        with self.assertRaises(PermissionError): self.service.tools.decision(wrong)
        self.service.tools.decision(data)
        with self.assertRaises(PermissionError): self.service.tools.check(self.browser, nonce, True, 'touch /tmp/different')
        self.assertEqual(self.service.tools.check(self.browser, nonce)['state'], 'approved')
        self.service.tools.check(self.browser, nonce, True, 'touch /tmp/approved')
        with self.assertRaises(PermissionError): self.service.tools.check(self.browser, nonce, True, 'touch /tmp/approved')
        with self.assertRaises(PermissionError): self.service.tools.decision(data)

    def test_locked_web_expiry_and_oversized(self):
        browser = 'b'*43
        with self.assertRaises(PermissionError): self.service.dispatch('/tool/request', {'browser':browser, 'command':'rm x'}, 0)
        with self.assertRaises(ValueError): self.service.tools.request(self.browser, 'x'*3001)
        ticket = self.service.tools.request(self.browser, 'rm x')
        with self.service.store.connect() as db:
            db.execute('UPDATE tool_approvals SET expires=0')
        with self.assertRaises(PermissionError): self.service.tools.decision(self.decision(ticket['nonce']))
        self.assertEqual(self.service.tools.check(self.browser, ticket['nonce'])['state'], 'expired')

if __name__ == '__main__': unittest.main()
