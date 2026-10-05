import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from natural_capture import classify
from local_capture import LocalCapture

class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.client=SimpleNamespace(base='http://example.invalid',chat_key='test-only',fallback_chain=lambda m:[m,'backup'])
        self.turns=[{'role':'user','content':'запиши пароль ддя сайта example.test пасс DummyOnly42'}]
    def output(self,d): return {'choices':[{'message':{'content':json.dumps(d)}}]}
    def test_typo_structured_contract(self):
        d={'action':'key','name':'example_test_password','value':'DummyOnly42','reply':'Сохранить пароль example.test?'}
        with patch('bot.json_request',return_value=self.output(d)) as req:
            self.assertEqual(classify(self.client,'primary',self.turns),d)
            payload=req.call_args.kwargs['payload']
            self.assertNotIn('tools',payload)
            self.assertEqual(payload['response_format'],{'type':'json_object'})
            self.assertEqual(req.call_args.kwargs['timeout'],12)
    def test_invented_secret_rejected(self):
        d={'action':'key','name':'example','value':'invented','reply':'Сохранить?'}
        with patch('bot.json_request',return_value=self.output(d)) as req:
            with self.assertRaises(ValueError): classify(self.client,'primary',self.turns)
            self.assertEqual(req.call_count,2)
    def test_reasoning_not_decision(self):
        with patch('bot.json_request',return_value={'choices':[{'message':{'reasoning_content':'{"action":"ignore"}'}}]}):
            with self.assertRaises(ValueError): classify(self.client,'primary',self.turns)
    def test_failover(self):
        with patch('bot.json_request',side_effect=[TimeoutError(),self.output({'action':'ignore'})]):
            self.assertEqual(classify(self.client,'primary',self.turns),{'action':'ignore'})
    def test_preserve_symbols(self):
        value='**_$`<>&,;'
        d={'action':'key','name':'example','value':value,'reply':'Сохранить?'}
        with patch('bot.json_request',return_value=self.output(d)):
            self.assertEqual(classify(self.client,'primary',[{'role':'user','content':'пароль '+value}])['value'],value)
    def test_invented_install_rejected(self):
        d={'action':'note','value':'Установлена Astra','reply':'Сохранить?'}
        with patch('bot.json_request',return_value=self.output(d)):
            with self.assertRaises(ValueError): classify(self.client,'primary',[{'role':'user','content':'Сегодня был релиз Astra'}])
    def test_free_text_not_json_rejected(self):
        with patch('bot.json_request',return_value={'choices':[{'message':{'content':'Я сохранил!'}}]}):
            with self.assertRaises(ValueError): classify(self.client,'primary',self.turns)

class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'vault.json'; self.path.write_text('{"keep":"unchanged"}')
        self.tg=Mock(); self.tg.call.return_value={'message_id':12}
        self.bot=SimpleNamespace(config=SimpleNamespace(admin_id=1),telegram=self.tg,litellm=Mock(),chat_model='test',vault_path=str(self.path),vault={},is_authorized=lambda a,c:a in (1,2))
        self.c=LocalCapture(self.bot); self.chat={'id':-1,'type':'supergroup','message_thread_id':77}
    def tearDown(self): self.tmp.cleanup()
    def test_repeated_turn_keeps_question_and_original(self):
        original='запиши пароль ддя сайта example.test пасс DummyOnly42'
        with patch('local_capture.classify',return_value={'action':'clarify','reply':'Это пароль входа на сайт?'}) as first:
            self.c.propose(1,self.chat,original)
        d={'action':'key','name':'example_test_password','value':'DummyOnly42','reply':'Вижу данные выше. Сохранить?'}
        with patch('local_capture.classify',return_value=d) as second:
            self.c.propose(1,self.chat,'я же написал')
            turns=second.call_args.args[2]
            self.assertEqual(turns[0]['content'],original)
            self.assertEqual(turns[1]['content'],'Это пароль входа на сайт?')
            self.assertEqual(turns[2]['content'],'я же написал')
        self.assertEqual(json.loads(self.path.read_text()),{'keep':'unchanged'})
        self.assertEqual(self.tg.call.call_args.args[1]['message_thread_id'],77)
        self.assertIn('Вижу данные выше',self.tg.call.call_args.args[1]['text'])
    def test_actor_topic_isolation(self):
        with patch('local_capture.classify',return_value={'action':'clarify','reply':'Какой сервис?'}) as plan:
            self.c.propose(1,self.chat,'private-marker')
            self.c.propose(2,self.chat,'hello')
            self.assertNotIn('private-marker',str(plan.call_args.args[2]))
            self.c.propose(1,{**self.chat,'message_thread_id':78},'another')
            self.assertNotIn('private-marker',str(plan.call_args.args[2]))
    def test_cancel_is_llm_text(self):
        with patch('local_capture.classify',return_value={'action':'cancel','reply':'Хорошо, оставляю без изменений.'}):
            self.c.propose(1,self.chat,'не нужно сохранять')
        self.assertEqual(self.tg.call.call_args.args[1]['text'],'Хорошо, оставляю без изменений.')
        self.assertFalse(self.c.clarifications)
    def test_normal_chat(self):
        with patch('local_capture.classify',return_value={'action':'ignore'}):
            self.assertFalse(self.c.propose(1,self.chat,'Как дела?'))
        self.assertFalse(self.c.drafts)
    def test_wrong_topic_confirmation(self):
        with patch('local_capture.classify',return_value={'action':'key','name':'test','value':'dummy','reply':'Сохранить?'}):
            self.c.propose(1,self.chat,'dummy')
        nonce=next(iter(self.c.drafts))
        self.c.callback({'id':'cb','data':'capture_yes:'+nonce,'from':{'id':1},'message':{'chat':self.chat,'message_id':12,'message_thread_id':78}})
        self.assertIn(nonce,self.c.drafts)

if __name__=='__main__': unittest.main()
