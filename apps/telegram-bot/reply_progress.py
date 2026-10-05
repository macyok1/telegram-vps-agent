"""Presentation-only Telegram status; never receives shell command contents."""
import logging
import threading
import time

LOG = logging.getLogger(__name__)

def chunks(text, limit=3800):
    current, units = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            yield ''.join(current)
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        yield ''.join(current)

class ReplyProgress:
    def __init__(self, telegram, chat_id):
        self.telegram, self.chat_id = telegram, chat_id
        self.message_id = None
        self.started = time.monotonic()
        self.phase = 'Обрабатываю запрос'
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def start(self):
        try:
            result = self.telegram.call('sendMessage', {
                'chat_id': self.chat_id, 'text': 'Обрабатываю запрос…'
            }, timeout=10)
            self.message_id = result.get('message_id')
        except Exception:
            LOG.warning('Progress creation unavailable')
        if self.message_id is not None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def report(self, _step, _command):
        # Tool arguments never leave the agent loop through this callback.
        self.phase = 'Проверяю данные'

    def _edit(self, text):
        self.telegram.call('editMessageText', {
            'chat_id': self.chat_id, 'message_id': self.message_id, 'text': text
        }, timeout=10)

    def tick(self):
        with self.lock:
            if self.stop.is_set() or self.message_id is None:
                return
            elapsed = int(time.monotonic() - self.started)
            try:
                self._edit(f'{self.phase}… {elapsed} с')
            except Exception:
                LOG.warning('Progress update unavailable')

    def _run(self):
        while not self.stop.wait(15):
            self.tick()

    def finish(self, answer):
        self.stop.set()
        # Serializes against an in-flight progress edit; no stale edit after final.
        with self.lock:
            parts = list(chunks(answer or 'Ответ модели пуст.'))
            edited = False
            if self.message_id is not None:
                try:
                    self._edit(parts[0])
                    edited = True
                except Exception:
                    LOG.warning('Final edit unavailable; sending answer normally')
            for part in parts[1:] if edited else parts:
                self.telegram.send_message(self.chat_id, part)
        if self.thread is not None:
            self.thread.join(timeout=1)
