"""Session-aware web channel attached to the bot, with separately approved tools."""
import grp
import json
import os
import re
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from console_client import ConsoleError, console_request
from web_chat_store import ChatStore
from tool_actions import run_agent_command
from command_policy import needs_approval

DB = '/var/lib/litellm-telegram-bot/web-chat.sqlite3'
DIRECTORY = '/run/admin-bot-chat'


def start_bridge(bot, system_prompt):
    store = ChatStore(DB, bot.chat_model)
    gid = grp.getgrnam('admin-panel').gr_gid
    os.makedirs(DIRECTORY, mode=0o750, exist_ok=True)
    os.chown(DIRECTORY, 0, gid)
    os.chmod(DIRECTORY, 0o750)

    def work(sid, browser):
        tool_ids = {}
        partial = {'id':None, 'text':'', 'saved':0}
        context = store.context(sid)
        def text_event(text):
            if text == '':
                if partial['id']:
                    store.tool_result(partial['id'], partial['text'], {'intermediate':True})
                partial.update(id=None, text='', saved=0)
                return
            partial['text'] = text
            if not partial['id']:
                partial['id'] = store.append(sid, 'assistant', text, {'streaming':True})
                partial['saved'] = time.monotonic()
            elif time.monotonic() - partial['saved'] >= 0.15:
                store.tool_result(partial['id'], text, {'streaming':True})
                partial['saved'] = time.monotonic()
        def tool_event(command, result=None):
            if result is None:
                tool_ids[command] = store.append(sid, 'tool', command,
                    {'command': command, 'state': 'running', 'verified': True})
            else:
                mid = tool_ids.pop(command, None)
                detail = {'command': command, 'state': 'finished', 'verified': True}
                if mid:
                    store.tool_result(mid, result, detail)

        def tool_runner(command):
            return run_agent_command(command, browser, lambda: store.stopped(sid))
        try:
            current = store.get(sid)
            try:
                enabled = console_request('/status', {'browser': browser})['unlocked']
            except ConsoleError:
                enabled = False
            prompt = system_prompt + (
                '\nСерверные инструменты доступны по подтверждению владельца. Не заявляй об успехе без результата shell.'
                if enabled else '\nСерверные инструменты ВЫКЛЮЧЕНЫ. Не выполняй и не заявляй о выполнении команд. Предложи открыть доступ через Telegram на странице консоли.')
            answer = bot.litellm.agent_chat(current['model'],
                [{'role': 'system', 'content': prompt}, *context],
                tools_enabled=enabled, tool_runner=tool_runner, on_tool=tool_event,
                should_cancel=lambda: store.stopped(sid), on_text=text_event, vault_path=bot.vault_path)
            if partial['id']:
                store.tool_result(partial['id'], answer, {})
            else:
                store.append(sid, 'assistant', answer)
            store.finish(sid)
        except Exception:
            if partial['id']:
                store.tool_result(partial['id'], partial['text'], {'interrupted':True})
            store.finish(sid, 'Не удалось получить ответ модели. Команды доступны отдельно в консоли; текст ошибки не означает выполнение действий.')

    class Handler(BaseHTTPRequestHandler):
        timeout = 8
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def handle_request(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                data = {}
                if self.command == 'POST':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 96000:
                        return self.reply(413, {'error': 'Запрос слишком большой.'})
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError('Требуется JSON-объект.')
                if path == '/sessions':
                    result = {'sessions': store.list(), 'default_model': bot.chat_model}
                    if self.command == 'POST':
                        result = store.create(data.get('model', bot.chat_model))
                    return self.reply(200, result)
                if path == '/models' and self.command == 'GET':
                    return self.reply(200, {'models': bot.litellm.models()})
                if path == '/search' and self.command == 'GET':
                    return self.reply(200, {'results':store.search(parse_qs(parsed.query).get('q', [''])[0])})
                export = re.fullmatch(r'/sessions/([a-f0-9]{32})/export', path)
                if export and self.command == 'GET':
                    return self.reply(200, store.export(export[1]))
                match = re.fullmatch(r'/sessions/([a-f0-9]{32})', path)
                if not match:
                    return self.reply(404, {'error': 'Маршрут не найден.'})
                sid = match[1]
                if self.command == 'GET':
                    return self.reply(200, store.get(sid, parse_qs(parsed.query).get('before', [None])[0]))
                action = data.get('action', 'message')
                if action == 'message':
                    text = data.get('text')
                    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                        raise ValueError('Сообщение: от 1 до 4000 символов.')
                    files = data.get('attachments', [])
                    if not isinstance(files, list) or len(files) > 3:
                        raise ValueError('Допускаются 3 текстовых файла.')
                    total = 0
                    for file in files:
                        if not isinstance(file, dict) or not isinstance(file.get('name'), str) or not isinstance(file.get('text'), str):
                            raise ValueError('Некорректное вложение.')
                        name, contents = file['name'], file['text']
                        if not 1 <= len(name) <= 120 or any(char in name for char in '\r\n\x00') or '\x00' in contents:
                            raise ValueError('Некорректный текстовый файл.')
                        total += len(contents)
                        if total > 6000:
                            raise ValueError('Вложения: не более 6000 символов суммарно.')
                        text += '\n\n[Текстовый файл: ' + name + ']\n' + contents
                    browser = data.get('browser', '')
                    if not isinstance(browser, str) or not 32 <= len(browser) <= 128:
                        raise ValueError('Некорректная сессия браузера.')
                    store.begin(sid, text.strip())
                    threading.Thread(target=work, args=(sid, browser), daemon=True).start()
                    return self.reply(202, {'accepted': True})
                store.update(sid, action, data.get('value'))
                return self.reply(200, {'ok': True})
            except (ValueError, TypeError):
                self.reply(400, {'error': 'Некорректный запрос.'})
            except KeyError:
                self.reply(404, {'error': 'Сессия не найдена.'})
            except RuntimeError as exc:
                self.reply(409, {'error': str(exc)})
            except Exception:
                self.reply(503, {'error': 'Бот временно недоступен.'})

        do_GET = handle_request
        do_POST = handle_request

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    path = DIRECTORY + '/bridge.sock'
    if os.path.exists(path):
        os.unlink(path)
    server = Server(path, Handler)
    os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
