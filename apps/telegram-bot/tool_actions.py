"""Agent commands pass through deterministic exact-command approval and jobs."""
import secrets
import time
from command_policy import needs_approval
from console_client import ConsoleError, console_request

def run_agent_command(command, browser=None, should_cancel=None):
    browser = browser or 'telegram-tool:' + secrets.token_urlsafe(24)
    payload = {'browser': browser, 'command': command}
    started = None
    try:
        if should_cancel and should_cancel():
            return 'Команда не запущена: запрос остановлен владельцем.'
        started = console_request('/tool/run', payload)
        while True:
            if should_cancel and should_cancel():
                console_request('/cancel', {'browser':browser, 'id':started['id']})
            job = console_request('/jobs', {'browser':browser, 'id':started['id']})
            if job['state'] != 'running':
                return f"exit={job['exit_code']}\n{job['stdout']}\n[stderr]\n{job['stderr']}\n{job['reason']}"[:12000]
            time.sleep(0.4)
    except ConsoleError as exc:
        return ('Результат недоступен, проверьте консоль: ' if started else 'Команда не запущена: ') + str(exc)

def handle_tool_callback(bot, callback):
    data = str(callback.get('data', ''))
    if not data.startswith('tc:'):
        return False
    actor = (callback.get('from') or {}).get('id')
    message = callback.get('message') or {}
    chat = message.get('chat') or {}
    cid = str(callback.get('id', ''))
    if actor != bot.config.admin_id or chat.get('id') != actor or chat.get('type') != 'private':
        bot.telegram.answer_callback(cid, 'Только владелец в личном чате.')
        return True
    parts = data.split(':', 2)
    try:
        if len(parts) != 3:
            raise ConsoleError('Некорректная кнопка.', 400)
        result = console_request('/tool/decision', {'action':parts[1], 'nonce':parts[2],
            'actor_id':actor, 'chat_id':chat['id'], 'message_id':message.get('message_id')})
        bot.telegram.answer_callback(cid, result['text'])
        bot.telegram.edit_message(chat['id'], message['message_id'], message.get('text', '') + '\n\n' + result['text'], reply_markup={'inline_keyboard':[]})
    except ConsoleError as exc:
        bot.telegram.answer_callback(cid, str(exc))
    return True
