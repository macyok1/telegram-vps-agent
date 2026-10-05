"""OpenAI-compatible SSE assembly; no tools execute until the stream completes."""
import json
import urllib.error
import urllib.request

def stream_chat(url, *, token, payload, timeout, on_text, should_cancel, error_type):
    body = dict(payload, stream=True)
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer ' + token, 'Content-Type':'application/json', 'Accept':'text/event-stream'})
    content, tools, finished = '', {}, False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                result = json.load(response)
                text = ((result.get('choices') or [{}])[0].get('message') or {}).get('content')
                if isinstance(text, str):
                    on_text(text)
                return result
            for raw in response:
                if should_cancel and should_cancel():
                    raise RuntimeError('Запрос остановлен владельцем.')
                if len(raw) > 262144:
                    raise RuntimeError('Слишком большой пакет модели.')
                line = raw.decode('utf-8').strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    finished = True
                    break
                chunk = json.loads(data)
                if chunk.get('error'):
                    raise error_type(0, 'Ошибка потока модели')
                for choice in chunk.get('choices') or []:
                    if choice.get('index', 0) != 0:
                        continue
                    if choice.get('finish_reason'):
                        finished = True
                    delta = choice.get('delta') or {}
                    if isinstance(delta.get('content'), str):
                        content += delta['content']
                        if len(content) > 48000:
                            raise RuntimeError('Превышен лимит ответа модели.')
                        on_text(content)
                    for part in delta.get('tool_calls') or []:
                        index = part.get('index', 0)
                        if not isinstance(index, int) or not 0 <= index < 16:
                            raise RuntimeError('Слишком много вызовов инструментов.')
                        tool = tools.setdefault(index, {'id':'', 'type':'function', 'function':{'name':'', 'arguments':''}})
                        if part.get('id'):
                            tool['id'] = part['id']
                        fn = part.get('function') or {}
                        for key in ['name', 'arguments']:
                            tool['function'][key] += fn.get(key) or ''
                        if len(tool['function']['arguments']) > 12000:
                            raise RuntimeError('Слишком длинная команда модели.')
            if not finished:
                raise error_type(0, 'Ответ модели оборвался')
        return {'choices':[{'message':{'content':content, 'tool_calls':[tools[index] for index in sorted(tools)]}}]}
    except urllib.error.HTTPError as exc:
        raise error_type(exc.code, 'Ошибка HTTP потока модели') from None
    except urllib.error.URLError:
        raise error_type(0, 'Сеть модели недоступна') from None
