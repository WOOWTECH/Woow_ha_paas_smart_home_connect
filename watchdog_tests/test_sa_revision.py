"""Pure checks for SA policy revision used to cut existing SSH streams."""
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'access_revocation', Path(__file__).resolve().parents[1] / 'access_revocation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
revision = module.security_access_policy_revision


class TestSaRevision(unittest.TestCase):
    def test_policy_removal_changes_revision(self):
        allowed = [{'id': 108, 'is_protected': True, 'access_publish_pending': False,
                    'access_policies': [{'id': 220, 'decision': 'allow',
                                         'include': [{'rule_type': 'email_list',
                                                      'rule_value': 'toypark1234@gmail.com'}]}]}]
        denied = [{'id': 108, 'is_protected': True, 'access_publish_pending': False,
                   'access_policies': []}]
        self.assertIsInstance(revision(allowed), int)
        self.assertNotEqual(revision(allowed), revision(denied))

    def test_order_does_not_trigger_reset(self):
        a = [{'id': 2, 'is_protected': True, 'access_publish_pending': False,
              'access_policies': [{'id': 8, 'decision': 'allow'}, {'id': 3, 'decision': 'deny'}]},
             {'id': 1, 'is_protected': False, 'access_publish_pending': False,
              'access_policies': []}]
        b = [{'access_policies': [], 'access_publish_pending': False,
              'is_protected': False, 'id': 1},
             {'access_policies': [{'decision': 'deny', 'id': 3},
                                  {'decision': 'allow', 'id': 8}],
              'access_publish_pending': False, 'is_protected': True, 'id': 2}]
        self.assertEqual(revision(a), revision(b))

    def test_incomplete_response_is_not_absence(self):
        for value in (None, {}, [{'id': 108, 'is_protected': True}],
                      [{'id': 108, 'access_policies': None}],
                      [{'id': True, 'access_policies': []}],
                      [{'id': 108, 'access_policies': []}]):
            with self.subTest(value=value):
                self.assertIsNone(revision(value))

    def test_token_denial_pending_does_not_block_stream_cutoff(self):
        """The pending flag may outlive CF policy publication and token revoke acceptance."""
        before = [{'id': 108, 'is_protected': True,
                   'access_publish_pending': True,
                   'access_policies': [{'id': 221, 'decision': 'allow'}]}]
        after = [{'id': 108, 'is_protected': True,
                  'access_publish_pending': True, 'access_policies': []}]
        self.assertNotEqual(revision(before), revision(after))


if __name__ == '__main__':
    unittest.main()
