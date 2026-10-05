"""Private console transport shared by the web app and Telegram bridge."""
import http.client
import json
import socket

SOCKET = '/run/admin-console/control.sock'


class ConsoleError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def console_request(path, payload):
    connection = http.client.HTTPConnection('localhost', timeout=8)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(8)
        connection.sock.connect(SOCKET)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        connection.request('POST', path, body, {'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ConsoleError('Ответ консоли слишком большой.')
        result = json.loads(raw)
        if response.status >= 400:
            raise ConsoleError(result.get('error', 'Команда отклонена.'), response.status)
        return result
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ConsoleError('Сервис консоли недоступен.') from exc
    finally:
        connection.close()
