#!/usr/bin/env python3
"""CI-only: check installed Linux imports, sockets and a harmless real broker job.

Run only on a disposable fresh host; creates test web-chat state.
No real Telegram or LLM API is called.
"""
from pathlib import Path
import os
import sys


def main():
    if os.geteuid() != 0:
        raise SystemExit('Smoke requires root on a disposable Linux host')
    sys.path.insert(0, '/opt/litellm-telegram-bot')
    from bot import Config, PersonalBot, build_agent_system_prompt
    from bot_chat_bridge import start_bridge
    from console_approval import handle_console_callback
    from local_capture import LocalCapture
    from tool_actions import run_agent_command

    config = Config('test-only', 42, 'http://test.invalid', 'test-only', 'test-only', 'bot-model', 1)
    bot = PersonalBot(config)
    bot.local_capture = LocalCapture(bot)
    bridge = start_bridge(bot, build_agent_system_prompt(bot.vault_path))
    try:
        assert Path('/run/admin-bot-chat/bridge.sock').is_socket()
        assert Path('/run/admin-console/control.sock').is_socket()
        assert handle_console_callback(bot, {'data': 'unrelated'}) is False
        output = run_agent_command('printf INSTALL_SMOKE_OK')
        assert 'exit=0\nINSTALL_SMOKE_OK' in output, 'Broker did not execute test command'
    finally:
        bridge.shutdown()
        bridge.server_close()
    print('OK: installed modules, root broker job and private bridge start without external API calls.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
