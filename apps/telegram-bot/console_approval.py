"""Deterministic, owner-only Telegram confirmation; never sent to an LLM."""
from console_client import ConsoleError, console_request


def handle_console_callback(bot, callback):
    data = str(callback.get('data', ''))
    if not data.startswith('cc:'):
        return False
    actor = (callback.get('from') or {}).get('id')
    message = callback.get('message') or {}
    chat = message.get('chat') or {}
    cid = str(callback.get('id', ''))
    if actor != bot.config.admin_id or chat.get('id') != bot.config.admin_id or chat.get('type') != 'private':
        bot.telegram.answer_callback(cid, 'Только владелец в личном чате.')
        return True
    parts = data.split(':', 2)
    if len(parts) != 3:
        bot.telegram.answer_callback(cid, 'Некорректная кнопка.')
        return True
    try:
        result = console_request('/decision', {'action': parts[1], 'nonce': parts[2],
            'actor_id': actor, 'chat_id': chat['id'], 'message_id': message.get('message_id')})
        bot.telegram.answer_callback(cid, result['text'])
        keyboard = {'inline_keyboard': []}
        if result.get('approved'):
            keyboard = {'inline_keyboard': [[{'text': 'Закрыть доступ', 'callback_data': 'cc:revoke:' + parts[2]}]]}
        bot.telegram.edit_message(chat['id'], message['message_id'],
            'Консоль веб-админки\n' + result['text'], reply_markup=keyboard)
    except ConsoleError as exc:
        bot.telegram.answer_callback(cid, str(exc))
    return True
