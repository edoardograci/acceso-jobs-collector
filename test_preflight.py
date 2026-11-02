import io
import json
import unittest
from unittest.mock import patch
import preflight

class PreflightTests(unittest.TestCase):
    def run_control(self, control):
        with patch.dict('os.environ', {'JOBS_INGEST_URL':'https://example.com/api/jobs-collector','JOBS_INGEST_TOKEN':'x'*32}), patch('preflight.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value=io.BytesIO(json.dumps({'control':control}).encode())
            preflight.check()

    def test_available_with_nullable_session(self):
        self.run_control({'active':None})

    def test_cooldown_stops_launch(self):
        with self.assertRaisesRegex(RuntimeError,'cooling down'):
            self.run_control({'cooldownUntil':9999999999999})

    def test_active_session_stops_launch(self):
        with self.assertRaisesRegex(RuntimeError,'session is active'):
            self.run_control({'active':{'until':9999999999999}})

    def test_missing_secret_never_connects(self):
        with patch.dict('os.environ',{},clear=True), patch('preflight.urllib.request.build_opener') as opener:
            with self.assertRaises(RuntimeError):preflight.check()
            opener.assert_not_called()

    def test_redirects_are_rejected(self):
        self.assertIsNone(preflight.NoRedirect().redirect_request(None,None,None,None,None,None))
