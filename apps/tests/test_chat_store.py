from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web_chat_store import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'chat.sqlite3'
        self.store = ChatStore(self.path, 'default-model')

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolated_context_and_models(self):
        a, b = self.store.create('model-a'), self.store.create('model-b')
        self.store.begin(a['id'], 'Only conversation A')
        self.store.begin(b['id'], 'Only conversation B')
        self.assertEqual(self.store.context(a['id']), [{'role':'user','content':'Only conversation A'}])
        self.assertEqual(self.store.get(b['id'])['model'], 'model-b')
        with self.assertRaises(RuntimeError): self.store.begin(a['id'], 'duplicate')
        self.store.finish(a['id'])
        self.store.update(a['id'], 'model', 'model-c')
        self.assertEqual(self.store.get(a['id'])['model'], 'model-c')

    def test_legacy_migration_and_restart_recovery(self):
        with self.store.connect() as db:
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,role TEXT,content TEXT)')
            db.execute("INSERT INTO messages(role,content) VALUES('user','legacy')")
        migrated = ChatStore(self.path, 'default-model')
        session = migrated.list()[0]
        self.assertEqual(migrated.context(session['id'])[0]['content'], 'legacy')
        migrated.begin(session['id'], 'new')
        restarted = ChatStore(self.path, 'default-model')
        self.assertFalse(restarted.get(session['id'])['busy'])
        self.assertIn('перезапуском', restarted.get(session['id'])['error'])
        self.assertEqual(len(restarted.list()), 1)
        restarted.update(session['id'], 'delete')
        self.assertEqual(ChatStore(self.path, 'default-model').list(), [])

    def test_tool_events_not_in_model_context_and_pagination(self):
        sid = self.store.create('model')['id']
        for number in range(70): self.store.append(sid, 'user', str(number))
        mid = self.store.append(sid, 'tool', 'id', {'state':'running'})
        self.store.tool_result(mid, 'exit=0\nuid=0', {'state':'finished','verified':True})
        latest = self.store.get(sid)
        self.assertEqual(len(latest['messages']), 60)
        self.assertTrue(latest['has_more'])
        older = self.store.get(sid, latest['messages'][0]['id'])
        self.assertEqual(len(older['messages']), 11)
        self.assertFalse(any(row['role'] == 'tool' for row in self.store.context(sid)))

    def test_rename_delete_and_stop(self):
        sid = self.store.create('model')['id']
        self.store.update(sid, 'rename', '<script>safe text</script>')
        self.store.begin(sid, 'test')
        self.store.update(sid, 'stop')
        self.assertTrue(self.store.stopped(sid))
        with self.assertRaises(RuntimeError): self.store.update(sid, 'delete')
        self.store.finish(sid)
        self.store.update(sid, 'delete')
        with self.assertRaises(KeyError): self.store.get(sid)

    def test_search_literal_wildcards_and_full_export(self):
        sid = self.store.create('model')['id']
        for number in range(70): self.store.append(sid, 'user', 'message ' + str(number))
        self.store.append(sid, 'user', 'literal 100%_match')
        self.assertEqual(len(self.store.export(sid)['messages']), 71)
        self.assertEqual(len(self.store.search('100%_')), 1)
        self.assertEqual(len(self.store.search('message')), 50)
        self.assertEqual(self.store.search("' OR 1=1 --"), [])


if __name__ == '__main__':
    unittest.main()
