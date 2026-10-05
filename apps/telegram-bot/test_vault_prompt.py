import inspect
import unittest
from unittest.mock import Mock
from collections import defaultdict, deque
import bot

class VaultPromptTests(unittest.TestCase):
    def test_default(self):
        prompt=bot.build_agent_system_prompt()
        self.assertIn('Основное хранилище секретов этого экземпляра бота: /var/lib/litellm-telegram-bot/vault.json.',prompt)
        self.assertIn('Отсутствие /root/vault.json не означает',prompt)
        self.assertIn('ПОЛНОСТЬЮ',prompt)

    def test_override(self):
        self.assertIn('Основное хранилище секретов этого экземпляра бота: /custom/vault.json.',bot.build_agent_system_prompt('/custom/vault.json'))

    def test_telegram_uses_effective_path(self):
        b=bot.PersonalBot.__new__(bot.PersonalBot)
        b.telegram=Mock()
        b.telegram.call.return_value={'message_id':1}
        b.litellm=Mock()
        b.litellm.agent_chat.return_value='test reply'
        b.chat_model='test'
        b.vault_path='/custom/vault.json'
        b.history=defaultdict(lambda:deque(maxlen=12))
        b.handle_text(1,'Где хранилище?')
        prompt=b.litellm.agent_chat.call_args.args[1][0]['content']
        self.assertEqual(prompt,bot.build_agent_system_prompt(b.vault_path))

    def test_bridge_wiring(self):
        self.assertIn('start_bridge(bot, build_agent_system_prompt(bot.vault_path))',inspect.getsource(bot.main))

if __name__=='__main__': unittest.main()
