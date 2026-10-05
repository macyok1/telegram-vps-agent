from types import SimpleNamespace
import unittest
import threading
from unittest.mock import Mock, patch
from console_approval import handle_console_callback


class ApprovalCallbackTests(unittest.TestCase):
    def test_approval_processed_while_model_message_is_blocked(self):
        from bot import PersonalBot
        started, release, approved = threading.Event(), threading.Event(), threading.Event()
        owner = SimpleNamespace(config=SimpleNamespace(admin_id=42), telegram=Mock(), stop_event=threading.Event())
        message = {'update_id':1, 'message':{'text':'slow model'}}
        callback = {'update_id':2, 'callback_query':{'data':'cc:approve:nonce'}}
        def updates(offset):
            if offset is None:
                yield message
                self.assertTrue(started.wait(2))
                yield callback
            else:
                owner.stop_event.wait(0.01)
        def handle(update):
            if 'message' in update:
                started.set()
                release.wait(3)
            else:
                approved.set()
                owner.stop_event.set()
        owner.telegram.get_updates.side_effect = updates
        owner.telegram.call.return_value = {}
        owner.handle_update = handle
        runner = threading.Thread(target=PersonalBot.run, args=(owner,))
        runner.start()
        try:
            self.assertTrue(approved.wait(2))
            self.assertFalse(release.is_set())
        finally:
            release.set()
            owner.stop_event.set()
            runner.join(3)

    def setUp(self):
        self.bot=SimpleNamespace(config=SimpleNamespace(admin_id=42),telegram=Mock())
        self.callback={'id':'callback','data':'cc:approve:random-nonce','from':{'id':42},
                       'message':{'message_id':41,'chat':{'id':42,'type':'private'}}}

    def test_only_owner_in_private_chat(self):
        with patch('console_approval.console_request') as broker:
            wrong=dict(self.callback, **{'from':{'id':43}})
            self.assertTrue(handle_console_callback(self.bot,wrong))
            wrong=dict(self.callback,message={'message_id':41,'chat':{'id':42,'type':'group'}})
            self.assertTrue(handle_console_callback(self.bot,wrong))
            broker.assert_not_called()

    def test_callback_is_deterministic_and_keeps_revoke_button(self):
        with patch('console_approval.console_request',return_value={'text':'Доступ открыт на 1 час.','approved':True}) as broker:
            self.assertTrue(handle_console_callback(self.bot,self.callback))
            self.assertEqual(broker.call_args.args[0],'/decision')
            self.assertEqual(broker.call_args.args[1]['message_id'],41)
            markup=self.bot.telegram.edit_message.call_args.kwargs['reply_markup']
            self.assertEqual(markup['inline_keyboard'][0][0]['callback_data'],'cc:revoke:random-nonce')

    def test_unrelated_callbacks_pass_through(self):
        self.assertFalse(handle_console_callback(self.bot,dict(self.callback,data='model:test:1')))


if __name__ == '__main__': unittest.main()
