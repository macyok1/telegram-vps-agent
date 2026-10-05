import copy
import unittest
from unittest.mock import Mock, patch
import bot


class AgentToolsTests(unittest.TestCase):
    def client(self):
        client=bot.LiteLLMClient.__new__(bot.LiteLLMClient)
        client.base='http://test.invalid'
        client.chat_key='test-only'
        client.timeout=1
        client.fallback_chain=lambda model:['first','second']
        return client

    @patch('bot.recall',return_value=None)
    @patch('bot.notes_context',return_value=None)
    def test_400_keeps_tools_on_fallback(self,*_):
        calls=[]
        def request(url,**kwargs):
            calls.append(copy.deepcopy(kwargs['payload']))
            if len(calls)==1: raise bot.ApiError(400,'tools not accepted')
            return {'choices':[{'message':{'content':'reply'}}]}
        with patch('bot.json_request',side_effect=request):
            self.assertEqual(self.client().agent_chat('first',[{'role':'user','content':'test'}]),'reply')
        self.assertTrue(all('tools' in payload for payload in calls))
        self.assertEqual(calls[-1]['model'],'second')

    @patch('bot.recall',return_value=None)
    @patch('bot.notes_context',return_value=None)
    def test_locked_chat_never_executes_even_if_model_returns_tool(self,*_):
        execute=Mock()
        replies=[{'choices':[{'message':{'tool_calls':[{'id':'a','function':{'name':'shell','arguments':'{"command":"id"}'}}]}}]},
                 {'choices':[{'message':{'content':'done'}}]}]
        with patch('bot.json_request',side_effect=replies):
            self.client().agent_chat('first',[{'role':'user','content':'test'}],tools_enabled=False,tool_runner=execute)
        execute.assert_not_called()

    @patch('bot.recall',return_value=None)
    @patch('bot.notes_context',return_value=None)
    def test_real_tool_result_is_reported(self,*_):
        execute=Mock(return_value='exit=0\nverified-output')
        event=Mock()
        replies=[{'choices':[{'message':{'tool_calls':[{'id':'a','function':{'name':'shell','arguments':'{"command":"id"}'}}]}}]},
                 {'choices':[{'message':{'content':'done'}}]}]
        with patch('bot.json_request',side_effect=replies):
            self.client().agent_chat('first',[{'role':'user','content':'test'}],tool_runner=execute,on_tool=event)
        execute.assert_called_once_with('id')
        self.assertEqual(event.call_args.args,('id','exit=0\nverified-output'))


if __name__ == '__main__': unittest.main()
