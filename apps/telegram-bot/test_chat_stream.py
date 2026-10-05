import io
import json
import unittest
from unittest.mock import patch
from chat_stream import stream_chat
from bot import ApiError

class StreamResponse(io.BytesIO):
    headers = {'Content-Type':'text/event-stream'}

def stream(*chunks, done=True):
    return StreamResponse((''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) +
                           ('data: [DONE]\n\n' if done else '')).encode())

class ChatStreamTests(unittest.TestCase):
    def call(self, response, callback, cancel=None):
        with patch('urllib.request.urlopen', return_value=response):
            return stream_chat('https://test.invalid', token='test', payload={}, timeout=1,
                on_text=callback, should_cancel=cancel, error_type=ApiError)

    def test_text_and_fragmented_tool_arguments(self):
        events=[]
        response=stream({'choices':[{'delta':{'content':'Hello '}}]},
            {'choices':[{'delta':{'content':'world', 'tool_calls':[{'index':0,'id':'one','function':{'name':'shell','arguments':'{"command":'}}]}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"whoami"}'}}]}, 'finish_reason':'tool_calls'}]})
        message=self.call(response, events.append)['choices'][0]['message']
        self.assertEqual(events, ['Hello ', 'Hello world'])
        self.assertEqual(message['tool_calls'][0]['function']['arguments'], '{"command":"whoami"}')

    def test_incomplete_stream_and_cancel_never_return_tools(self):
        chunk={'choices':[{'delta':{'tool_calls':[{'index':0,'id':'one','function':{'name':'shell','arguments':'{"command":"rm x"}'}}]}}]}
        with self.assertRaises(ApiError): self.call(stream(chunk, done=False), lambda _:None)
        with self.assertRaises(RuntimeError): self.call(stream(chunk), lambda _:None, lambda:True)

if __name__ == '__main__': unittest.main()
