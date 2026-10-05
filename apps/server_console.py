"""Root-only command broker with owner approval through a private Telegram chat."""
import grp
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import pwd
import secrets
import socket
import socketserver
import sqlite3
import struct
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

from console_jobs import CommandJobs, owner_key
from command_policy import needs_approval
from tool_approval import ToolApprovals

DIRECTORY = '/run/admin-console'
STATE = '/var/lib/admin-console/control.sqlite3'
LOG = logging.getLogger('admin-console')


def read_env(path):
    values = {}
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class ConsoleStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS approvals (
                    nonce TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL,
                    expires REAL, state TEXT, message_id INTEGER, label TEXT);
                CREATE TABLE IF NOT EXISTS grants (owner TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS attempts (created REAL, owner TEXT);
            ''')
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def allowed(self, browser):
        with self.connect() as db:
            row = db.execute('SELECT expires FROM grants WHERE owner=?', (owner_key(browser),)).fetchone()
        return bool(row and row['expires'] > time.time())


class ConsoleService:
    def __init__(self, store, password, telegram_token, admin_id, web_uid, send=None):
        self.store, self.password = store, password
        self.token, self.admin_id, self.web_uid = telegram_token, int(admin_id), web_uid
        self.send = send or self._send
        self.jobs = CommandJobs(store, store.allowed)
        self.tools = ToolApprovals(store, self.send, self.admin_id)

    def _send(self, payload):
        req = urllib.request.Request(f'https://api.telegram.org/bot{self.token}/sendMessage',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=6) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Не удалось отправить подтверждение в Telegram.')
        return result['result']['message_id']

    def status(self, browser):
        owner, now = owner_key(browser), time.time()
        with self.store.connect() as db:
            grant = db.execute('SELECT expires FROM grants WHERE owner=?', (owner,)).fetchone()
            pending = db.execute("SELECT label,expires FROM approvals WHERE owner=? AND state='pending' AND expires>? ORDER BY created DESC LIMIT 1", (owner, now)).fetchone()
        expires = grant['expires'] if grant else 0
        return {'unlocked': expires > now, 'expires': expires,
                'pending': dict(pending) if pending else None, 'server_time': now}

    def request_approval(self, browser, data):
        owner, now = owner_key(browser), time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE created<?', (now - 600,))
            recent = db.execute('SELECT COUNT(*) FROM attempts WHERE created>?', (now - 60,)).fetchone()[0]
            own = db.execute('SELECT COUNT(*) FROM attempts WHERE owner=? AND created>?', (owner, now - 30)).fetchone()[0]
            if recent >= 10 or own:
                raise RuntimeError('Подождите 30 секунд перед следующим запросом.')
            db.execute('INSERT INTO attempts VALUES(?,?)', (now, owner))
        supplied = data.get('password', '')
        if not isinstance(supplied, str) or not self.password or not hmac.compare_digest(supplied.encode(), self.password.encode()):
            raise PermissionError('Неверный пароль владельца.')
        nonce = secrets.token_urlsafe(18)
        label = hashlib.sha256(nonce.encode()).hexdigest()[:6].upper()
        with self.store.connect() as db:
            db.execute("UPDATE approvals SET state='superseded' WHERE owner=? AND state='pending'", (owner,))
            db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?)',
                       (nonce, owner, now, now + 300, 'pending', None, label))
        try:
            message_id = self.send({'chat_id': self.admin_id,
                'text': f'Консоль веб-админки · запрос {label}\n\n'
                        'Разрешить этому браузеру команды ROOT и серверные инструменты чата на 1 час?\n'
                        'Полный доступ к VPS. Подтвердите только запрос, который открыли сами.\n'
                        'Код должен совпадать с кодом в админке. Кнопка действует 5 минут.',
                'reply_markup': {'inline_keyboard': [[
                    {'text': 'Подтверждаю · 1 час', 'callback_data': 'cc:approve:' + nonce},
                    {'text': 'Отказать', 'callback_data': 'cc:deny:' + nonce}]]}})
            with self.store.connect() as db:
                db.execute('UPDATE approvals SET message_id=? WHERE nonce=?', (message_id, nonce))
        except Exception:
            with self.store.connect() as db:
                db.execute("UPDATE approvals SET state='failed' WHERE nonce=?", (nonce,))
            raise RuntimeError('Telegram недоступен. Доступ не выдан.') from None
        LOG.info('approval requested label=%s', label)
        return self.status(browser)

    def decision(self, data):
        if data.get('actor_id') != self.admin_id or data.get('chat_id') != self.admin_id:
            raise PermissionError('Подтверждать может только владелец в личном чате.')
        action = data.get('action')
        if action not in {'approve', 'deny', 'revoke'}:
            raise ValueError('Неизвестное действие.')
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM approvals WHERE nonce=?', (data.get('nonce'),)).fetchone()
            if not row or row['message_id'] != data.get('message_id'):
                raise PermissionError('Запрос не найден.')
            if action == 'revoke' and row['state'] == 'approved':
                db.execute('DELETE FROM grants WHERE owner=?', (row['owner'],))
                db.execute("UPDATE approvals SET state='revoked' WHERE nonce=?", (row['nonce'],))
                return {'text': 'Доступ закрыт. Выполняющиеся команды будут остановлены.'}
            if row['state'] != 'pending' or row['expires'] <= now:
                raise PermissionError('Кнопка уже использована или истекла.')
            db.execute('UPDATE approvals SET state=? WHERE nonce=?',
                       ('approved' if action == 'approve' else 'denied', row['nonce']))
            if action == 'approve':
                db.execute('INSERT OR REPLACE INTO grants VALUES(?,?)', (row['owner'], now + 3600))
        LOG.info('approval label=%s action=%s', row['label'], action)
        return {'text': 'Доступ открыт на 1 час.' if action == 'approve' else 'Запрос отклонён.',
                'nonce': row['nonce'], 'approved': action == 'approve', 'label': row['label']}

    def dispatch(self, path, data, uid):
        if uid not in {0, self.web_uid}:
            raise PermissionError('Недопустимый клиент.')
        if path == '/decision':
            if uid != 0:
                raise PermissionError('Веб-приложение не может подтверждать доступ.')
            return self.decision(data)
        if path == '/tool/decision':
            if uid != 0:
                raise PermissionError('Веб-процесс не может подтверждать команды.')
            return self.tools.decision(data)
        if path.startswith('/tool/') and uid != 0:
            raise PermissionError('Только агент может создавать заявки на команды.')
        browser = data.get('browser', '')
        if not isinstance(browser, str) or not 32 <= len(browser) <= 128:
            raise ValueError('Недействительная сессия браузера.')
        if path == '/status':
            return self.status(browser)
        if path == '/request':
            return self.request_approval(browser, data)
        if path == '/lock':
            with self.store.connect() as db:
                db.execute('DELETE FROM grants WHERE owner=?', (owner_key(browser),))
                db.execute("UPDATE approvals SET state='revoked' WHERE owner=? AND state='pending'", (owner_key(browser),))
            return {'ok': True}
        if path == '/cancel':
            return self.jobs.cancel(browser, data.get('id'))
        telegram_tool = browser.startswith('telegram-tool:')
        if telegram_tool and uid != 0:
            raise PermissionError('Недопустимый канал.')
        if path.startswith('/tool/'):
            if not telegram_tool and not self.store.allowed(browser):
                raise PermissionError('Доступ к инструментам истёк или отозван.')
            if path == '/tool/request':
                return self.tools.request(browser, data.get('command'))
            if path == '/tool/status':
                return self.tools.check(browser, data.get('nonce'))
            if path == '/tool/run':
                command = data.get('command')
                if telegram_tool:
                    with self.store.connect() as db:
                        db.execute('INSERT OR REPLACE INTO grants VALUES(?,?)', (owner_key(browser), time.time() + 300))
                return self.jobs.start(browser, command)
            raise KeyError('Маршрут не найден.')
        if not self.store.allowed(browser):
            raise PermissionError('Подтвердите доступ в Telegram. Разрешение отсутствует или истекло.')
        if path == '/run':
            return self.jobs.start(browser, data.get('command'), data.get('timeout', 120))
        if path == '/jobs':
            return self.jobs.get(browser, data.get('id'))
        raise KeyError('Маршрут не найден.')


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(name)s %(levelname)s %(message)s')
    bot_env = read_env('/etc/litellm-telegram-bot.env')
    admin_env = read_env('/etc/admin-panel.env')
    gid = grp.getgrnam('admin-panel').gr_gid
    service = ConsoleService(ConsoleStore(STATE), admin_env.get('CODEX_ADMIN_PASSWORD'),
        bot_env['TELEGRAM_BOT_TOKEN'], bot_env['TELEGRAM_ADMIN_ID'], pwd.getpwnam('admin-panel').pw_uid)

    class Handler(BaseHTTPRequestHandler):
        timeout = 8
        def log_message(self, *args):
            pass

        def do_POST(self):
            status = 200
            try:
                _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 24000:
                    raise ValueError('Недопустимый размер запроса.')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Требуется JSON-объект.')
                result = service.dispatch(self.path, data, uid)
            except PermissionError as exc:
                status, result = 403, {'error': str(exc)}
            except (ValueError, TypeError) as exc:
                status, result = 400, {'error': str(exc)}
            except KeyError:
                status, result = 404, {'error': 'Не найдено.'}
            except RuntimeError as exc:
                status, result = 409, {'error': str(exc)}
            except Exception:
                status, result = 503, {'error': 'Сервис временно недоступен.'}
            raw = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    os.makedirs(DIRECTORY, mode=0o750, exist_ok=True)
    os.chown(DIRECTORY, 0, gid)
    os.chmod(DIRECTORY, 0o750)
    path = DIRECTORY + '/control.sock'
    if os.path.exists(path):
        os.unlink(path)
    server = ThreadedUnixServer(path, Handler)
    os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    server.serve_forever()


if __name__ == '__main__':
    serve()
