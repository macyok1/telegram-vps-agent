#!/usr/bin/env python3
"""Scan tracked files without printing matched secrets; fail closed on private data."""
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = (
    re.compile(rb'-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY'),
    re.compile(rb'\b\d{7,12}:[A-Za-z0-9_-]{25,}\b'),
    re.compile(rb'\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{20,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
)
FORBIDDEN = {'vault.json', 'allowed.json', 'server-notes.json', 'capture-pending.json',
             'auth.json', 'credentials-and-instructions.md', 'mastersecrets.gpg', '.env'}


def main():
    repo = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=repo).decode().split('\0')
    failures = []
    for name in filter(None, tracked):
        path = repo / name
        if path.name in FORBIDDEN or path.suffix in {'.gpg', '.pem', '.key', '.sqlite3', '.db', '.zip'} or (path.suffix == '.env'):
            failures.append(name + ': private file type')
            continue
        raw = path.read_bytes()
        if any(pattern.search(raw) for pattern in PATTERNS):
            failures.append(name + ': secret-like value')
        if re.search(rb'\b(?:94[.]141[.]160[.]234|13[.]143[.]129[.]220|admin[.]macyok[.]com)\b', raw):
            failures.append(name + ': original deployment identifier')
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print('OK: tracked files exclude private runtime data, known secret shapes and original server addresses.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
