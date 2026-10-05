import unittest
from unittest.mock import Mock
from collections import defaultdict, deque
from reply_progress import ReplyProgress, chunks
from bot import PersonalBot

class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tg = Mock()
        self.tg.call.return_value = {'message_id': 42}

    def test_single_message_replaced_and_secret_exact(self):
        p = ReplyProgress(self.tg, 7).start()
        secret = 'Test-only: **ab_$`<>&**\nпароль'
        p.report(5, 'DO-NOT-SHOW command test-secret')
        p.tick()
        p.finish(secret)
        calls=self.tg.call.call_args_list
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in calls),1)
        self.assertEqual(calls[-1].args[1]['text'],secret)
        self.assertEqual(calls[-1].args[1]['message_id'],42)
        self.assertNotIn('DO-NOT-SHOW',str(calls))
        self.tg.send_message.assert_not_called()
        self.assertFalse(p.thread.is_alive())

    def test_no_update_after_final(self):
        p=ReplyProgress(self.tg,7).start()
        p.finish('done')
        n=self.tg.call.call_count
        p.tick()
        self.assertEqual(n,self.tg.call.call_count)

    def test_long_unicode_lossless(self):
        answer='😀Пароль_**`'*1500
        p=ReplyProgress(self.tg,7).start()
        p.finish(answer)
        first=self.tg.call.call_args.args[1]['text']
        all_parts=[first]+[c.args[1] for c in self.tg.send_message.call_args_list]
        self.assertEqual(''.join(all_parts),answer)
        self.assertTrue(all(len(s.encode('utf-16-le'))//2<=3800 for s in all_parts))

    def test_start_failure(self):
        self.tg.call.side_effect=RuntimeError('private-error')
        p=ReplyProgress(self.tg,7).start()
        p.finish('answer')
        self.tg.send_message.assert_called_once_with(7,'answer')
        self.assertIsNone(p.thread)

    def test_edit_failure_fallback(self):
        p=ReplyProgress(self.tg,7).start()
        self.tg.call.side_effect=RuntimeError('private-error')
        p.finish('answer')
        self.tg.send_message.assert_called_once_with(7,'answer')
        self.assertFalse(p.thread.is_alive())

    def make_bot(self):
        b=PersonalBot.__new__(PersonalBot)
        b.telegram=self.tg
        b.litellm=Mock()
        b.chat_model='test-model'
        b.history=defaultdict(lambda:deque(maxlen=12))
        return b

    def test_integration(self):
        b=self.make_bot()
        def answer(*args,**kwargs):
            kwargs['on_progress'](1,'DO-NOT-SHOW')
            return 'Test-only password: ab_*`'
        b.litellm.agent_chat.side_effect=answer
        b.handle_text(7,'Найди доступ для теста')
        self.assertEqual(self.tg.call.call_args.args[1]['text'],'Test-only password: ab_*`')
        self.assertEqual(len(b.history[7]),2)
        self.tg.send_message.assert_not_called()

    def test_model_failure_replaces_status(self):
        b=self.make_bot()
        b.litellm.agent_chat.side_effect=RuntimeError('secret-error')
        b.handle_text(7,'Найди доступ для теста')
        self.assertIn('Не удалось',self.tg.call.call_args.args[1]['text'])
        self.assertNotIn('secret-error',str(self.tg.call.call_args_list))
        self.tg.send_message.assert_not_called()

    def test_partial_send_failure_stops_worker(self):
        p=ReplyProgress(self.tg,7).start()
        self.tg.send_message.side_effect=RuntimeError('network')
        with self.assertRaises(RuntimeError): p.finish('a'*8000)
        self.assertTrue(p.stop.is_set())

if __name__=='__main__':
    unittest.main()
