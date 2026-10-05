"""Bounded command jobs. No model, Telegram polling, or web server dependency."""
import hashlib
import logging
import os
import selectors
import signal
import subprocess
import threading
import time
import uuid

LOG = logging.getLogger('admin-console')
OUTPUT_LIMIT = 262144


def owner_key(browser):
    return hashlib.sha256(browser.encode()).hexdigest()


class CommandJobs:
    def __init__(self, store, allowed, cwd='/root'):
        self.store = store
        self.allowed = allowed
        self.cwd = cwd
        self.lock = threading.Lock()
        self.running = {}
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, command TEXT NOT NULL,
                created REAL, state TEXT, stdout TEXT DEFAULT '', stderr TEXT DEFAULT '',
                exit_code INTEGER, reason TEXT DEFAULT '', truncated INTEGER DEFAULT 0)''')
            db.execute("UPDATE jobs SET state='interrupted',reason='Сервис перезапущен' WHERE state='running'")

    def start(self, browser, command, timeout=120):
        if not isinstance(command, str) or not 1 <= len(command.strip()) <= 8000 or '\x00' in command:
            raise ValueError('Команда должна содержать от 1 до 8000 символов.')
        timeout = int(timeout)
        if not 5 <= timeout <= 300:
            raise ValueError('Таймаут: от 5 до 300 секунд.')
        owner = owner_key(browser)
        with self.lock:
            if len(self.running) >= 2 or any(item['owner'] == owner for item in self.running.values()):
                raise RuntimeError('Дождитесь завершения текущей команды.')
            jid = uuid.uuid4().hex
            item = {'owner': owner, 'cancel': threading.Event()}
            self.running[jid] = item
            with self.store.connect() as db:
                db.execute("INSERT INTO jobs(id,owner,command,created,state) VALUES(?,?,?,?,'running')",
                           (jid, owner, command, time.time()))
            threading.Thread(target=self._run, args=(jid, browser, command, timeout, item), daemon=True).start()
        return {'id': jid}

    def get(self, browser, jid=None):
        with self.store.connect() as db:
            if jid:
                row = db.execute('SELECT * FROM jobs WHERE owner=? AND id=?', (owner_key(browser), jid)).fetchone()
                if not row:
                    raise KeyError('Команда не найдена.')
                return dict(row)
            rows = db.execute('''SELECT id,command,created,state,exit_code,reason,truncated
                FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 50''', (owner_key(browser),)).fetchall()
            return {'jobs': [dict(row) for row in rows]}

    def cancel(self, browser, jid):
        self.get(browser, jid)
        with self.lock:
            item = self.running.get(jid)
            if item:
                item['cancel'].set()
        return {'ok': True}

    def _save(self, jid, stdout, stderr, **extra):
        values = {'stdout': stdout.decode('utf-8', 'replace'), 'stderr': stderr.decode('utf-8', 'replace'), **extra}
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', (*values.values(), jid))

    def _run(self, jid, browser, command, timeout, item):
        proc = None
        selector = selectors.DefaultSelector()
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        reason = ''
        truncated = 0
        exit_code = -1
        try:
            # Never inherit gateway/Telegram credentials from the service environment.
            proc = subprocess.Popen(['/bin/bash', '-c', command], cwd=self.cwd,
                env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                     'HOME': '/root', 'LANG': 'C.UTF-8', 'TERM': 'dumb'},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
            for name in output:
                pipe = getattr(proc, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout
            last_save = 0
            while selector.get_map():
                if item['cancel'].is_set():
                    reason = 'Остановлено владельцем'
                elif not self.allowed(browser):
                    reason = 'Доступ отозван или истёк'
                elif time.monotonic() >= deadline:
                    reason = 'Превышен таймаут'
                if reason:
                    break
                for key, _ in selector.select(0.15):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = OUTPUT_LIMIT - len(output[key.data])
                    output[key.data].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = 1
                        reason = 'Превышен лимит вывода'
                        break
                if time.monotonic() - last_save > 0.4:
                    self._save(jid, **output)
                    last_save = time.monotonic()
            if reason:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            exit_code = proc.wait(timeout=1)
        except Exception:
            reason = reason or 'Ошибка запуска команды'
        finally:
            # Kill descendants too, including processes left behind by an exited bash.
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                if exit_code == -1:
                    exit_code = proc.returncode
                for name in output:
                    getattr(proc, name).close()
            selector.close()
            with self.lock:
                self.running.pop(jid, None)
            self._save(jid, **output, state='finished', exit_code=exit_code, reason=reason, truncated=truncated)
            LOG.info('job=%s finished exit=%s reason=%s', jid, exit_code, reason or 'complete')
