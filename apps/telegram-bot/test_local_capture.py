import json
import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from local_capture import LocalCapture,parse,notes_context

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'vault.json'
        self.path.write_text('{"old":"keep"}')
        tg=Mock(); tg.call.return_value={'message_id':12}
        self.bot=SimpleNamespace(config=SimpleNamespace(admin_id=1),telegram=tg,vault_path=str(self.path),vault={},is_authorized=lambda actor,chat: actor==1)
        self.c=LocalCapture(self.bot)
        self.chat={'id':1,'type':'private'}
        self.bot.litellm=Mock()
        self.bot.chat_model='test'
        def planner(client, model, turns, names):
            try: item=parse(turns[-1]['content'])
            except ValueError: item=None
            if item is None or (item[0]=='note' and 'password:' in item[2]):
                return {'action':'clarify','reply':'Уточни сервис.'}
            return {'action':item[0],'name':item[1],'value':item[2],'reply':'Предлагаю сохранить.'}
        self.planner=patch('local_capture.classify',side_effect=planner)
        self.planner.start()
    def tearDown(self):
        self.planner.stop()
        self.tmp.cleanup()
    def proposal(self,text='сохрани ключ new: test-only'):
        self.assertTrue(self.c.propose(1,self.chat,text))
        return next(iter(self.c.drafts))
    def callback(self,n,actor=1,action='capture_yes'):
        return self.c.callback({'id':'cb','data':action+':'+n,'from':{'id':actor},'message':{'message_id':12,'chat':self.chat}})
    def test_no_write_before_confirm(self):
        self.proposal(); self.assertEqual(json.loads(self.path.read_text()),{'old':'keep'})
    def test_exact_value_parse(self):
        value=' a`**$\nnext '
        self.assertEqual(parse('сохрани ключ key: '+value)[2],value)
    def test_unauthorized_cannot_consume(self):
        n=self.proposal(); self.callback(n,actor=2); self.assertIn(n,self.c.drafts)
    def test_cancel(self):
        n=self.proposal(); self.callback(n,action='capture_no'); self.assertNotIn(n,self.c.drafts)
        self.assertEqual(json.loads(self.path.read_text()),{'old':'keep'})
    def test_expired(self):
        n=self.proposal(); self.c.drafts[n]['expires']=0
        with patch.object(self.c,'commit') as commit:
            self.callback(n)
            commit.assert_called_once()
    def test_group_block(self):
        self.c.propose(1,{'id':3,'type':'group'},'сохрани ключ key: dummy')
        self.assertTrue(self.c.drafts)
    def test_ambiguous(self):
        self.assertTrue(self.c.propose(1,self.chat,'сохрани ключ'))
        self.assertFalse(self.c.drafts)
    def test_sensitive_note(self):
        self.c.propose(1,self.chat,'запомни о сервере: password: dummy')
        self.assertFalse(self.c.drafts)
    @unittest.skipIf(os.name=='nt','fcntl deployment target is Linux')
    def test_commit_preserves_others_and_replay(self):
        n=self.proposal(); self.callback(n); self.callback(n)
        self.assertEqual(json.loads(self.path.read_text()),{'old':'keep','new':'test-only'})
        self.assertEqual(len(list(self.path.parent.glob('vault.before-capture-*'))),1)
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
    @unittest.skipIf(os.name=='nt','fcntl deployment target is Linux')
    def test_conflicting_update(self):
        n=self.proposal('сохрани ключ old: replacement')
        self.path.write_text('{"old":"changed"}')
        self.callback(n); self.assertEqual(json.loads(self.path.read_text())['old'],'changed')
    @unittest.skipIf(os.name=='nt','fcntl deployment target is Linux')
    def test_notes_dedup_and_context(self):
        for i in range(2):
            n=self.proposal('запомни о сервере: test server note'); self.callback(n)
        notes=json.loads(self.path.with_name('server-notes.json').read_text())
        self.assertEqual(len(notes),1)
        with patch.dict(os.environ,{'VAULT_PATH':str(self.path)}):
            self.assertIn('test server note',notes_context()['content'])

if __name__=='__main__': unittest.main()
