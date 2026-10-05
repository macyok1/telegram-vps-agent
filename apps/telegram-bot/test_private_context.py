import json
from pathlib import Path
import tempfile
import unittest
from private_context import owner_context

class PrivateContextTests(unittest.TestCase):
    def test_complete_context_is_not_filtered_by_exact_service_name_and_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            vault=Path(directory)/'vault.json'
            document=Path(directory)/'notes.md'
            vault.write_text(json.dumps({'hosting_portal':{'description':'Xorek account','password':'test-only'},
                                        'unrelated':'second-entry'}),encoding='utf-8')
            document.write_text('Хорёк — кабинет хостинга, hosting_portal.',encoding='utf-8')
            text=owner_context(vault,document)['content']
            self.assertIn('test-only',text)
            self.assertIn('second-entry',text)
            self.assertIn('Хорёк',text)
            vault.write_text('{"new":"changed"}',encoding='utf-8')
            text=owner_context(vault,document)['content']
            self.assertIn('changed',text)
            self.assertNotIn('test-only',text)

if __name__ == '__main__': unittest.main()
