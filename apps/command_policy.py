"""Conservative read-only fast path; everything else requires exact approval."""
import re
import shlex

READ_ONLY = {'pwd', 'whoami', 'id', 'uptime', 'df', 'free', 'ls',
             'stat', 'cat', 'head', 'tail', 'wc', 'uname', 'readlink'}

def needs_approval(command):
    if not isinstance(command, str) or re.search(r'[;&|<>`$\\\n\r\x00]', command):
        return True
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    if not words:
        return True
    name = words[0]
    if name in READ_ONLY:
        return False
    if name == 'date':
        return any(word not in {'-I', '-Is', '-u', '--utc', '--iso-8601', '--iso-8601=seconds'} and not word.startswith('+') for word in words[1:])
    if name == 'hostname':
        return any(word not in {'-f', '-s', '-d', '-i', '-I', '--fqdn'} for word in words[1:])
    if name == 'journalctl':
        return any(word.startswith(('--vacuum', '--rotate', '--flush', '--sync', '--relinquish', '--setup-keys', '--update-catalog')) for word in words[1:])
    if name == 'systemctl':
        return len(words) < 2 or words[1] not in {'status', 'is-active', 'is-enabled', 'show', 'list-units', 'list-unit-files', 'list-timers'}
    # Unknown executables, interpreters, nested shells, sudo and pipelines are gated.
    return True
