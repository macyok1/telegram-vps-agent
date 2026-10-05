import json
import os
import unittest
from unittest.mock import patch,Mock
import memory_recall as m
import bot

class RecallTests(unittest.TestCase):
    def test_disabled(self):
        with patch.dict(os.environ,{'BOT_SUPERMEMORY_RECALL':'0'}), patch.object(m.urllib.request,'urlopen') as req:
            self.assertIsNone(m.recall([{'role':'user','content':'сервер'}])); req.assert_not_called()
    def test_unrelated(self):
        with patch.dict(os.environ,{'BOT_SUPERMEMORY_RECALL':'1'}), patch.object(m.urllib.request,'urlopen') as req:
            self.assertIsNone(m.recall([{'role':'user','content':'Привет'}])); req.assert_not_called()
    def test_fixed_query_and_scope(self):
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'results':[{'id':'1','memory':'VPS fact'},{'id':'2','memory':'password: test-only'}]}).encode()
        with patch.dict(os.environ,{'BOT_SUPERMEMORY_RECALL':'1'}),patch.object(m.Path,'read_text',return_value='{"supermemory_opencode_api_key":"test-only"}'),patch.object(m.urllib.request,'urlopen',return_value=response) as req:
            result=m.recall([{'role':'user','content':'сервер секрет-не-отправлять'}])
            data=json.loads(req.call_args.args[0].data)
            self.assertEqual(data['containerTag'],'sm_project_default')
            self.assertEqual(data['q'],m.QUERY)
            self.assertNotIn('секрет-не-отправлять',str(data))
            self.assertIn('VPS fact',result['content']); self.assertNotIn('test-only',result['content'])
    def test_failure_is_explicit(self):
        with patch.dict(os.environ,{'BOT_SUPERMEMORY_RECALL':'1'}),patch.object(m.Path,'read_text',side_effect=OSError):
            self.assertIn('недоступен',m.recall([{'role':'user','content':'сервер'}])['content'])

    def test_context_reaches_model_without_overwriting_owner(self):
        client=bot.LiteLLMClient.__new__(bot.LiteLLMClient)
        client.base='http://test.invalid'
        client.chat_key='test-only'
        client.timeout=1
        messages=[{'role':'system','content':'system'}, {'role':'user','content':'сервер'}]
        context={'role':'user','content':'Untrusted memory evidence'}
        with patch.object(client,'fallback_chain',return_value=['test']), patch.object(bot,'recall',return_value=context), patch.object(bot,'json_request',return_value={'choices':[{'message':{'content':'answer'}}]}) as request:
            self.assertEqual(client.agent_chat('test',messages),'answer')
            sent=request.call_args.kwargs['payload']['messages']
            self.assertEqual(sent[0],messages[0])
            self.assertEqual(sent[-1],messages[-1])
            self.assertEqual(sent[1],context)
            self.assertEqual(len(messages),2)

    def test_network_error_does_not_fail_recall(self):
        with patch.dict(os.environ,{'BOT_SUPERMEMORY_RECALL':'1'}),patch.object(m.Path,'read_text',return_value='{"supermemory_opencode_api_key":"test-only"}'),patch.object(m.urllib.request,'urlopen',side_effect=TimeoutError):
            self.assertIn('недоступен',m.recall([{'role':'user','content':'сервер'}])['content'])

if __name__=='__main__': unittest.main()
