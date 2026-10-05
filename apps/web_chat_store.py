"""Durable conversations, isolated context, and verified tool events."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import time
import uuid


class ChatStore:
    def __init__(self, path, default_model):
        self.path = str(path)
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT, model TEXT, created REAL,
                    updated REAL, busy INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    stopped INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT,
                    content TEXT, created REAL, detail TEXT DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS chat_session_messages ON chat_messages(session_id,id);
                CREATE TABLE IF NOT EXISTS chat_migrations (name TEXT PRIMARY KEY);
            ''')
            db.execute("UPDATE sessions SET busy=0,error='Ответ прерван перезапуском. Отправьте запрос заново.' WHERE busy=1")
            legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
            if legacy and not db.execute("SELECT 1 FROM chat_migrations WHERE name='legacy_messages'").fetchone() and not db.execute('SELECT 1 FROM sessions LIMIT 1').fetchone():
                rows = db.execute('SELECT role,content FROM messages ORDER BY id').fetchall()
                if rows:
                    sid, now = uuid.uuid4().hex, time.time()
                    db.execute('INSERT INTO sessions(id,title,model,created,updated) VALUES(?,?,?,?,?)',
                               (sid, 'Предыдущий веб-чат', default_model, now, now))
                    db.executemany('INSERT INTO chat_messages(session_id,role,content,created) VALUES(?,?,?,?)',
                                   [(sid, row['role'], row['content'], now) for row in rows])
            if legacy:
                db.execute("INSERT OR IGNORE INTO chat_migrations VALUES('legacy_messages')")
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

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM sessions ORDER BY updated DESC LIMIT 200')]

    def create(self, model):
        if not isinstance(model, str) or not 1 <= len(model) <= 160:
            raise ValueError('Некорректная модель.')
        sid, now = uuid.uuid4().hex, time.time()
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= 200:
                raise ValueError('Достигнут лимит 200 сессий. Удалите ненужные.')
            db.execute('INSERT INTO sessions(id,title,model,created,updated) VALUES(?,?,?,?,?)',
                       (sid, 'Новый чат', model, now, now))
        return self.get(sid)

    def get(self, sid, before=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row:
                raise KeyError('Сессия не найдена.')
            result = dict(row)
            before = int(before or 9223372036854775807)
            rows = db.execute('SELECT * FROM chat_messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 61',
                              (sid, before)).fetchall()
        result['has_more'] = len(rows) > 60
        result['messages'] = [dict(item, detail=json.loads(item['detail'])) for item in reversed(rows[:60])]
        return result

    def context(self, sid):
        with self.connect() as db:
            rows = db.execute("SELECT role,content FROM chat_messages WHERE session_id=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT 24", (sid,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def append(self, sid, role, content, detail=None):
        with self.connect() as db:
            cursor = db.execute('INSERT INTO chat_messages(session_id,role,content,created,detail) VALUES(?,?,?,?,?)',
                (sid, role, content[:12000], time.time(), json.dumps(detail or {}, ensure_ascii=False)))
            db.execute('UPDATE sessions SET updated=? WHERE id=?', (time.time(), sid))
        return cursor.lastrowid

    def tool_result(self, mid, content, detail):
        with self.connect() as db:
            db.execute('UPDATE chat_messages SET content=?,detail=? WHERE id=?',
                       (content[:12000], json.dumps(detail, ensure_ascii=False), mid))

    def export(self, sid):
        with self.connect() as db:
            session = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not session:
                raise KeyError('Сессия не найдена.')
            rows = db.execute('SELECT * FROM chat_messages WHERE session_id=? ORDER BY id LIMIT 10001', (sid,)).fetchall()
        if len(rows) > 10000:
            raise ValueError('Слишком большая история для одного экспорта.')
        if sum(len(row['content']) + len(row['detail']) for row in rows) > 600000:
            raise ValueError('Экспорт превышает лимит размера. Разделите историю.')
        result = dict(session)
        result['messages'] = [dict(row, detail=json.loads(row['detail'])) for row in rows]
        return result

    def search(self, query):
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
            raise ValueError('Поиск: от 2 до 100 символов.')
        pattern = '%' + query.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT s.id,s.title,m.id AS message_id,
                substr(m.content,1,240) AS preview FROM chat_messages m JOIN sessions s ON s.id=m.session_id
                WHERE m.content LIKE ? ESCAPE '\\' ORDER BY m.id DESC LIMIT 50''', (pattern,))]

    def begin(self, sid, text):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row:
                raise KeyError('Сессия не найдена.')
            if row['busy'] or db.execute('SELECT COUNT(*) FROM sessions WHERE busy=1').fetchone()[0] >= 2:
                raise RuntimeError('Дождитесь завершения текущего запроса.')
            db.execute("UPDATE sessions SET busy=1,stopped=0,error='',title=?,updated=? WHERE id=?",
                       (text[:70] if row['title'] == 'Новый чат' else row['title'], time.time(), sid))
            db.execute('INSERT INTO chat_messages(session_id,role,content,created) VALUES(?,?,?,?)',
                       (sid, 'user', text[:12000], time.time()))

    def update(self, sid, action, value=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not current:
                raise KeyError('Сессия не найдена.')
            if action == 'stop':
                db.execute('UPDATE sessions SET stopped=1 WHERE id=?', (sid,))
            elif current['busy']:
                raise RuntimeError('Сначала остановите запрос и дождитесь завершения.')
            elif action == 'delete':
                db.execute('DELETE FROM chat_messages WHERE session_id=?', (sid,))
                db.execute('DELETE FROM sessions WHERE id=?', (sid,))
            elif action in {'rename', 'model'}:
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= (100 if action == 'rename' else 160):
                    raise ValueError('Некорректное значение.')
                column = 'title' if action == 'rename' else 'model'
                db.execute(f'UPDATE sessions SET {column}=?,updated=? WHERE id=?', (value.strip(), time.time(), sid))
            else:
                raise ValueError('Неизвестное действие.')

    def stopped(self, sid):
        with self.connect() as db:
            row = db.execute('SELECT stopped FROM sessions WHERE id=?', (sid,)).fetchone()
        return not row or bool(row['stopped'])

    def finish(self, sid, error=''):
        with self.connect() as db:
            db.execute('UPDATE sessions SET busy=0,error=?,updated=? WHERE id=?', (error, time.time(), sid))
