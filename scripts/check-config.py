#!/usr/bin/env python3
"""Validate an installed bot without printing secrets or invoking model tools."""
from pathlib import Path
import json
import sys
import urllib.error
import urllib.request


def read_env(path):
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            name, value = line.split('=', 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def request(url, key=None, payload=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def main():
    env_file = Path('/etc/litellm-telegram-bot.env')
    try:
        env = read_env(env_file)
        for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_ADMIN_ID', 'LITELLM_CHAT_KEY', 'LITELLM_MASTER_KEY', 'LITELLM_BOT_MODEL'):
            if not env.get(name) or 'REPLACE_' in env[name]:
                raise ValueError('Заполните ' + name)
        if int(env['TELEGRAM_ADMIN_ID']) <= 0:
            raise ValueError('TELEGRAM_ADMIN_ID: нужен положительный ID пользователя')
        for path in (env_file, Path('/etc/admin-panel.env'), Path(env['VAULT_PATH'])):
            if path.stat().st_mode & 0o077:
                raise ValueError('Права должны исключать группу и остальных: ' + str(path))
        vault = json.loads(Path(env['VAULT_PATH']).read_text(encoding='utf-8'))
        if not isinstance(vault, dict):
            raise ValueError('Vault должен быть JSON-объектом')
        telegram = request('https://api.telegram.org/bot' + env['TELEGRAM_BOT_TOKEN'] + '/getMe')
        if not telegram.get('ok'):
            raise ValueError('Telegram не подтвердил токен')
        webhook = request('https://api.telegram.org/bot' + env['TELEGRAM_BOT_TOKEN'] + '/getWebhookInfo')
        if webhook.get('result', {}).get('url'):
            raise ValueError('У бота активен webhook: отключите его перед long polling')
        base = env.get('LITELLM_INTERNAL_URL', 'http://127.0.0.1:4000').rstrip('/')
        request(base + '/health/liveliness')
        models = request(base + '/v1/models', env['LITELLM_CHAT_KEY'])
        if env['LITELLM_BOT_MODEL'] not in {item.get('id') for item in models.get('data', [])}:
            raise ValueError('LITELLM_BOT_MODEL отсутствует в /v1/models')
        # Проверка текстового ответа; shell tools и данные vault не передаются.
        reply = request(base + '/v1/chat/completions', env['LITELLM_CHAT_KEY'], {
            'model': env['LITELLM_BOT_MODEL'], 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
            'max_tokens': 128,
        })
        if not reply.get('choices'):
            raise ValueError('Модель не вернула choices')
        print('OK: конфигурация, права, пустой/валидный vault, Telegram, модели и текстовый запрос.')
        print('Tool calling и реальный диалог проверяются отдельно после старта.')
        return 0
    except urllib.error.HTTPError as error:
        print('Проверка API не прошла: HTTP ' + str(error.code) + '. Секреты и ответ не выводятся.', file=sys.stderr)
    except (OSError, ValueError, KeyError):
        # URL ошибки Telegram может содержать токен, поэтому не печатаем exception.
        print('Проверка не прошла. Проверьте заполнение env, ID, права файлов, webhook, сеть и имя модели.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
