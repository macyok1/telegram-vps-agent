import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from local_capture import LocalCapture

class AutoSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'vault.json';self.path.write_text('{"keep":"old"}')
        self.tg=Mock();self.tg.call.return_value={'message_id':12}
        self.bot=SimpleNamespace(config=SimpleNamespace(admin_id=1),telegram=self.tg,litellm=Mock(),chat_model='test',vault_path=str(self.path),vault={},is_authorized=lambda a,c:a==1,stop_event=threading.Event())
        self.c=LocalCapture(self.bot);self.chat={'id':-1,'type':'supergroup'}
    def tearDown(self):self.tmp.cleanup()
    def propose(self,kind='note',value='11 авг др',name='test'):
        with patch('local_capture.classify',return_value={'action':kind,'value':value,'name':name,'reply':'Сохранить?'}):
            self.c.propose(1,self.chat,value)
        return next(reversed(self.c.drafts))
    def test_wait_then_save_once(self):
        with patch('local_capture.time.time',return_value=100): n=self.propose()
        self.assertEqual(self.c.drafts[n]['expires'],700)
        with patch.object(self.c,'commit') as commit:
            with patch('local_capture.time.time',return_value=699):self.c.process_due()
            commit.assert_not_called()
            with patch('local_capture.time.time',return_value=700):self.c.process_due();self.c.process_due()
            commit.assert_called_once()
        self.assertFalse(self.c.drafts)
    def test_cancel(self):
        n=self.propose();self.c.callback({'id':'cb','data':'capture_no:'+n,'from':{'id':1},'message':{'chat':self.chat,'message_id':12}})
        with patch.object(self.c,'commit') as commit:
            self.c.process_due();commit.assert_not_called()
        self.assertEqual(json.loads(self.c.queue_path.read_text()),{})
    def test_restore_on_restart(self):
        n=self.propose()
        restored=LocalCapture(self.bot)
        self.assertEqual(restored.drafts[n],self.c.drafts[n])
        self.assertEqual(self.c.queue_path.stat().st_mode&0o777,0o600) if os.name!='nt' else None
    def test_unrelated_turn_keeps_pending(self):
        n=self.propose()
        with patch('local_capture.classify',return_value={'action':'ignore'}):self.c.propose(1,self.chat,'Как дела?')
        self.assertIn(n,self.c.drafts)
    def test_different_notes_both_pending(self):
        self.propose(value='first note');self.propose(value='second note')
        self.assertEqual(len(self.c.drafts),2)
    def test_revoked_access(self):
        n=self.propose();self.c.drafts[n]['expires']=0
        self.bot.is_authorized=lambda a,c:False
        with patch.object(self.c,'commit') as commit:
            self.c.process_due();commit.assert_not_called()
    @unittest.skipIf(os.name=='nt','Linux fcntl')
    def test_real_note_auto(self):
        n=self.propose();self.c.drafts[n]['expires']=0;self.c.process_due()
        notes=json.loads(self.path.with_name('server-notes.json').read_text())
        self.assertEqual(next(iter(notes.values()))['text'],'11 авг др')
        self.assertEqual(json.loads(self.path.read_text()),{'keep':'old'})
    @unittest.skipIf(os.name=='nt','Linux fcntl')
    def test_key_conflict(self):
        n=self.propose(kind='key',value='replacement',name='keep');self.c.drafts[n]['expires']=0
        self.path.write_text('{"keep":"newer"}')
        self.c.process_due()
        self.assertEqual(json.loads(self.path.read_text())['keep'],'newer')
    def test_button_timer_serialized(self):
        n=self.propose();self.c.drafts[n]['expires']=0
        with patch.object(self.c,'commit') as commit:
            threads=[threading.Thread(target=self.c.process_due),threading.Thread(target=lambda:self.c.callback({'id':'cb','data':'capture_yes:'+n,'from':{'id':1},'message':{'chat':self.chat,'message_id':12}}))]
            for t in threads:t.start()
            for t in threads:t.join()
            commit.assert_called_once()

if __name__=='__main__':unittest.main()
