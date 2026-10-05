"""Persistent one-command approvals, decided only by the private Telegram owner."""
import secrets
import time
from console_jobs import owner_key

class ToolApprovals:
    def __init__(self, store, send, admin_id):
        self.store, self.send, self.admin_id = store, send, admin_id
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_approvals (
                nonce TEXT PRIMARY KEY, owner TEXT, command TEXT, expires REAL,
                state TEXT, message_id INTEGER)''')

    def request(self, browser, command):
        if not isinstance(command, str) or not 1 <= len(command) <= 8000 or '\x00' in command:
            raise ValueError('Некорректная команда.')
        # Telegram limit must not hide any suffix of an approved command.
        if len(command) > 3000:
            raise ValueError('Команда слишком длинная для безопасного подтверждения. Разбейте её на отдельные команды.')
        nonce, now = secrets.token_urlsafe(18), time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute("SELECT COUNT(*) FROM tool_approvals WHERE state='pending' AND expires>?", (now,)).fetchone()[0]
            if count >= 4:
                raise RuntimeError('Слишком много ожидающих команд.')
            db.execute('INSERT INTO tool_approvals VALUES(?,?,?,?,?,?)',
                       (nonce, owner_key(browser), command, now + 300, 'pending', None))
        try:
            mid = self.send({'chat_id': self.admin_id,
                'text': 'Бот просит выполнить команду ROOT:\n\n' + command +
                        '\n\nРазрешение только на этот текст, один запуск. Кнопка действует 5 минут.',
                'reply_markup': {'inline_keyboard': [[
                    {'text':'Выполнить эту команду', 'callback_data':'tc:approve:' + nonce},
                    {'text':'Отказать', 'callback_data':'tc:deny:' + nonce}]]}})
            with self.store.connect() as db:
                db.execute('UPDATE tool_approvals SET message_id=? WHERE nonce=?', (mid, nonce))
        except Exception:
            with self.store.connect() as db:
                db.execute("UPDATE tool_approvals SET state='failed' WHERE nonce=?", (nonce,))
            raise RuntimeError('Telegram недоступен. Команда не выполнена.') from None
        return {'nonce': nonce, 'state': 'pending'}

    def decision(self, data):
        if data.get('actor_id') != self.admin_id or data.get('chat_id') != self.admin_id:
            raise PermissionError('Только владелец в личном чате.')
        if data.get('action') not in {'approve', 'deny'}:
            raise ValueError('Некорректное действие.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tool_approvals WHERE nonce=?', (data.get('nonce'),)).fetchone()
            if not row or row['message_id'] != data.get('message_id') or row['state'] != 'pending' or row['expires'] <= time.time():
                raise PermissionError('Кнопка использована или истекла.')
            state = 'approved' if data['action'] == 'approve' else 'denied'
            db.execute('UPDATE tool_approvals SET state=? WHERE nonce=?', (state, row['nonce']))
        return {'text': 'Команда разрешена один раз.' if state == 'approved' else 'Команда отклонена.'}

    def check(self, browser, nonce, consume=False, command=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tool_approvals WHERE nonce=? AND owner=?', (nonce, owner_key(browser))).fetchone()
            if not row:
                raise PermissionError('Заявка не найдена.')
            state = row['state'] if row['expires'] > time.time() else 'expired'
            if consume:
                if state != 'approved' or row['command'] != command:
                    raise PermissionError('Нет подтверждения точного текста команды.')
                db.execute("UPDATE tool_approvals SET state='used' WHERE nonce=?", (nonce,))
            return {'state': state}
