"""Worker integration with fake SDK/conversion; never authenticates or sends."""
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from delivery_queue import RetryLater


def load_worker():
    dotenv = ModuleType('dotenv')
    dotenv.load_dotenv = lambda *a: None
    ringcentral = ModuleType('ringcentral')
    ringcentral.SDK = Mock()
    processor = ModuleType('process_print_job')
    processor.process_job = Mock()
    processor.extract_metadata = lambda raw: {'fax': raw.decode()}
    spec = importlib.util.spec_from_file_location('worker_under_test',
                                                Path(__file__).resolve().parents[1] / 'fax_worker.py')
    worker = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'dotenv': dotenv, 'ringcentral': ringcentral,
                                 'process_print_job': processor}):
        spec.loader.exec_module(worker)
    return worker


class ApiError(Exception):
    """Mirror ringcentral.http.api_exception.ApiException's public shape."""
    def __init__(self, status, retry_after=None):
        self.raw_response = SimpleNamespace(status_code=status, headers={})
        if retry_after is not None:
            self.raw_response.headers['Retry-After'] = retry_after
        super().__init__('request details Authorization: SECRET')

    def api_response(self):
        return SimpleNamespace(response=lambda: self.raw_response)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = load_worker()
        self.worker.log = lambda message: None

    def test_sdk_http_status_and_retry_after(self):
        error = ApiError(429, '123')
        self.assertEqual(self.worker.http_status(error), 429)
        self.assertEqual(self.worker.get_retry_after(error), 123)
        self.assertEqual(self.worker.get_retry_after(ApiError(429, 'bad')), 65)

    def test_rate_limit_does_not_immediately_repeat_post(self):
        self.worker.send_fax = Mock(side_effect=ApiError(429, '123'))
        with self.assertRaises(RetryLater) as caught:
            self.worker.send_with_retry(None, None, '+19035263662', 'fax.pdf')
        self.assertEqual(caught.exception.delay, 123)
        self.assertEqual(self.worker.send_fax.call_count, 1)

    def test_401_reauthenticates_but_durable_scheduler_controls_resubmission(self):
        self.worker.send_fax = Mock(side_effect=ApiError(401))
        platform = SimpleNamespace(login=Mock())
        with self.assertRaises(RetryLater):
            self.worker.send_with_retry(None, platform, '+19035263662', 'fax.pdf')
        self.assertEqual(platform.login.call_count, 1)
        self.assertEqual(self.worker.send_fax.call_count, 1)

    def test_5xx_and_network_failures_are_not_retried_or_logged_with_headers(self):
        for error in (ApiError(503), TimeoutError('Authorization: SECRET')):
            self.worker.send_fax = Mock(side_effect=error)
            with self.assertRaises(RuntimeError) as caught:
                self.worker.send_with_retry(None, None, '+19035263662', 'fax.pdf')
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(self.worker.send_fax.call_count, 1)

    def test_error_text_is_not_treated_as_http_status(self):
        self.assertIsNone(self.worker.http_status(RuntimeError('429 401')))

    def test_status_error_does_not_expose_request_headers(self):
        platform = SimpleNamespace(get=Mock(side_effect=ApiError(503)))
        with self.assertRaises(RuntimeError) as caught:
            self.worker.fetch_delivery(platform, '12345')
        self.assertNotIn('SECRET', str(caught.exception))

    def test_rejected_sdk_oauth_refresh_reauthenticates(self):
        error = ApiError(400)
        error.raw_response.request = SimpleNamespace(url='https://platform.ringcentral.com/restapi/oauth/token')
        self.worker.send_fax = Mock(side_effect=error)
        platform = SimpleNamespace(login=Mock())
        with self.assertRaises(RetryLater):
            self.worker.send_with_retry(None, platform, '+19035263662', 'fax.pdf')
        self.assertEqual(platform.login.call_count, 1)

    def test_fax_http_400_does_not_trigger_oauth_or_duplicate_submission(self):
        self.worker.send_fax = Mock(side_effect=ApiError(400))
        platform = SimpleNamespace(login=Mock())
        with self.assertRaises(RuntimeError):
            self.worker.send_with_retry(None, platform, '+19035263662', 'fax.pdf')
        platform.login.assert_not_called()

    def configure_spool(self, root):
        self.worker.SPOOL_DIR = root
        for attr, name in [('PENDING_DIR', 'pending'), ('PROCESSING_DIR', 'processing'),
                           ('COMPLETED_DIR', 'completed'), ('FAILED_DIR', 'failed')]:
            path = root / name
            path.mkdir()
            setattr(self.worker, attr, path)
        pdf = root / 'converted.pdf'
        pdf.write_bytes(b'pdf')
        self.worker.process_job = Mock(return_value={'fax': '9035263662', 'fax_pdf': pdf})
        self.worker.create_platform = Mock(return_value=(object(), object()))
        self.worker.send_with_retry = Mock(return_value='12345')

    def test_worker_once_retains_active_then_restart_confirms_and_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.configure_spool(root)
            (root / 'pending' / 'job.raw').write_bytes(b'9035263662')
            with patch.object(sys, 'argv', ['fax_worker.py', '--send', '--once']):
                self.assertEqual(self.worker.main(), 0)
                self.assertTrue((root / 'processing' / 'job.raw').exists())
                self.assertFalse((root / 'completed' / 'job.raw').exists())
                db_path = root / 'delivery-state' / 'queue.sqlite3'
                with sqlite3.connect(db_path) as db:
                    db.execute('UPDATE jobs SET next_poll=0')
                self.worker.fetch_delivery = Mock(return_value={
                    'messageStatus': 'Sent', 'to': [{'phoneNumber': '+19035263662'}]})
                self.assertEqual(self.worker.main(), 0)
            self.assertEqual(self.worker.send_with_retry.call_count, 1)
            self.assertTrue((root / 'completed' / 'job.raw').exists())
            self.worker.process_job.assert_called_once_with(b'9035263662', send=False)

    def test_dry_run_returns_original_to_pending_without_auth_or_send(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.configure_spool(root)
            (root / 'pending' / 'job.raw').write_bytes(b'9035263662')
            with patch.object(sys, 'argv', ['fax_worker.py', '--once']):
                self.worker.main()
            self.worker.create_platform.assert_not_called()
            self.worker.send_with_retry.assert_not_called()
            self.assertTrue((root / 'pending' / 'job.raw').exists())
            self.assertFalse((root / 'completed' / 'job.raw').exists())

    def test_claim_refuses_to_overwrite_existing_processing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.configure_spool(root)
            pending = root / 'pending' / 'job.raw'
            pending.write_bytes(b'new job')
            processing = root / 'processing' / 'job.raw'
            processing.write_bytes(b'original job')
            with self.assertRaises(RuntimeError):
                self.worker.claim_job(pending)
            self.assertEqual(processing.read_bytes(), b'original job')
            self.assertTrue(pending.exists())


if __name__ == '__main__':
    unittest.main()
