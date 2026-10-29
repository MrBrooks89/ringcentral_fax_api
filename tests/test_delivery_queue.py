import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from delivery_queue import DeliveryQueue, RetryLater, acquire_lock, delivery_result, normalize_number


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spool = Path(self.temp.name)
        for name in ('pending', 'processing', 'completed', 'failed'):
            (self.spool / name).mkdir()
        self.now = 1000
        self.sent = []
        self.responses = {}
        self.logs = []
        self.queue = self.open_queue()
        self.addCleanup(lambda: self.queue.close())

    def open_queue(self):
        return DeliveryQueue(self.spool, self.submit, self.fetch, log=self.logs.append,
                             clock=lambda: self.now)

    def submit(self, number, pdf):
        self.assertEqual(Path(pdf).read_bytes(), b'fax content')
        self.sent.append(number)
        return str(len(self.sent))

    def fetch(self, message_id):
        response = self.responses.get(message_id)
        if isinstance(response, Exception):
            raise response
        return response

    def add_job(self, name='a.raw', number='9035263662'):
        raw = self.spool / 'processing' / name
        raw.write_bytes(b'raw')
        pdf = self.spool / 'source.pdf'
        pdf.write_bytes(b'fax content')
        self.queue.register(raw, number, pdf)
        return raw

    def row(self, name='a.raw'):
        return next(row for row in self.queue.rows() if row['job'] == name)

    def result(self, status, error=None, number='+19035263662'):
        return {'messageStatus': status, 'to': [{'phoneNumber': number,
                'messageStatus': status, 'faxErrorCode': error}]}

    def poll(self, status='Sent', error=None, message_id='1'):
        self.responses[message_id] = self.result(status, error)
        self.now += 30
        self.queue.poll_one()

    def test_submission_does_not_mean_completed(self):
        raw = self.add_job()
        self.queue.submit_one()
        self.assertEqual(self.row()['state'], 'active')
        self.assertTrue(raw.exists())
        self.assertFalse((self.spool / 'completed' / raw.name).exists())

    def test_success_archives_original_and_releases_document(self):
        self.add_job()
        self.queue.submit_one()
        pinned = self.row()['pdf']
        self.poll()
        self.assertEqual(self.row()['state'], 'completed')
        self.assertTrue((self.spool / 'completed' / 'a.raw').exists())
        self.assertFalse(Path(pinned).exists())

    def test_same_destination_blocks_but_other_number_proceeds(self):
        self.add_job()
        self.add_job('b.raw', '(903) 526-3662')
        self.add_job('c.raw', '5126286857')
        self.queue.submit_one()
        self.now += 7
        self.queue.submit_one()
        self.assertEqual(self.sent, ['+19035263662', '+15126286857'])
        self.assertEqual(self.row('b.raw')['state'], 'ready')

    def test_busy_retries_only_after_delay_then_succeeds(self):
        self.add_job()
        self.add_job('b.raw')
        self.queue.submit_one()
        self.poll('SendingFailed', 'LineBusy')
        self.assertEqual(self.row()['state'], 'retry')
        self.now += 119
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)
        self.now += 1
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 2)
        self.poll(message_id='2')
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 3)

    def test_exactly_five_retries_then_failed(self):
        self.add_job()
        for attempt in range(1, 7):
            self.queue.submit_one()
            self.poll('SendingFailed', 'LineBusy', str(attempt))
            self.now += 120
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 6)
        self.assertEqual(self.row()['state'], 'failed')
        self.assertEqual(self.queue.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 6)

    def test_zero_retries_config(self):
        self.queue.max_busy_retries = 0
        self.add_job()
        self.queue.submit_one()
        self.poll('SendingFailed', 'LineBusy')
        self.assertEqual(self.row()['state'], 'failed')

    def test_queued_with_busy_code_is_not_resubmitted(self):
        self.add_job()
        self.queue.submit_one()
        self.poll('Queued', 'LineBusy')
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.row()['state'], 'active')

    def test_unknown_partial_and_missing_error_need_review(self):
        for status, error in [('Unknown', None), ('SendingFailed', None),
                              ('SendingFailed', 'Undefined'), ('SendingFailed', 'SentPartially')]:
            with self.subTest(status=status, error=error):
                self.queue.db.execute('DELETE FROM attempts')
                self.queue.db.execute('DELETE FROM jobs')
                self.queue.db.commit()
                self.now += 120
                self.add_job()
                self.queue.submit_one()
                self.poll(status, error, str(len(self.sent)))
                self.assertEqual(self.row()['state'], 'review')
                old_count = len(self.sent)
                self.now += 200
                self.queue.submit_one()
                self.assertEqual(len(self.sent), old_count)

    def test_nonbusy_failure_not_retried(self):
        self.add_job()
        self.queue.submit_one()
        self.poll('SendingFailed', 'NoFaxMachine')
        self.assertEqual(self.row()['state'], 'failed')
        self.now += 500
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)

    def test_restart_polls_existing_message_without_resend(self):
        self.add_job()
        self.queue.submit_one()
        self.queue.close()
        self.queue = self.open_queue()
        self.queue.recover(lambda raw: '9035263662')
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)
        self.poll()
        self.assertEqual(self.row()['state'], 'completed')

    def test_restart_preserves_busy_delay_and_attempt_count(self):
        self.add_job()
        self.queue.submit_one()
        self.poll('SendingFailed', 'LineBusy')
        self.queue.close()
        self.queue = self.open_queue()
        self.queue.recover(lambda raw: '9035263662')
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)
        self.now += 120
        self.queue.submit_one()
        self.assertEqual(self.row()['attempts'], 2)

    def test_interrupted_post_is_held_and_blocks_destination(self):
        self.add_job()
        self.queue.update('a.raw', state='submitting', attempts=1)
        self.queue.close()
        self.queue = self.open_queue()
        self.queue.recover(lambda raw: '9035263662')
        self.add_job('b.raw')
        self.queue.submit_one()
        self.assertEqual(self.row()['state'], 'review')
        self.assertEqual(self.sent, [])

    def test_timeout_after_post_does_not_resend(self):
        self.add_job()
        def timeout(*args):
            raise TimeoutError('Response lost')
        self.queue.submit = timeout
        self.queue.submit_one()
        self.assertEqual(self.row()['state'], 'review')
        self.queue.submit = self.submit
        self.now += 200
        self.queue.submit_one()
        self.assertEqual(self.sent, [])

    def test_missing_id_is_uncertain(self):
        self.add_job()
        self.queue.submit = lambda *args: None
        self.queue.submit_one()
        self.assertEqual(self.row()['state'], 'review')

    def test_confirmed_rejection_defers_without_spending_busy_retry(self):
        self.add_job()
        def rejected(*args):
            raise RetryLater(65, 'HTTP 429')
        self.queue.submit = rejected
        self.queue.submit_one()
        self.assertEqual(self.row()['attempts'], 0)
        self.assertEqual(self.row()['state'], 'ready')
        self.queue.submit = self.submit
        self.now += 64
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 0)
        self.now += 1
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)

    def test_status_failure_never_triggers_resend(self):
        self.add_job()
        self.queue.submit_one()
        self.responses['1'] = TimeoutError('GET failed')
        self.now += 30
        self.queue.poll_one()
        self.queue.submit_one()
        self.assertEqual(self.row()['state'], 'active')
        self.assertEqual(len(self.sent), 1)
        self.now += 1800
        self.queue.poll_one()
        self.assertEqual(self.row()['state'], 'review')
        self.poll()
        self.assertEqual(self.row()['state'], 'completed')

    def test_status_rate_limit_pauses_other_submissions(self):
        self.add_job()
        self.queue.submit_one()
        self.add_job('b.raw', '5126286857')
        self.responses['1'] = RetryLater(65, 'HTTP 429')
        self.now += 30
        self.queue.poll_one()
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)
        self.now += 65
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 2)

    def test_untracked_processing_file_needs_review(self):
        raw = self.spool / 'processing' / 'legacy.raw'
        raw.write_bytes(b'raw')
        self.queue.recover(lambda raw: '9035263662')
        self.queue.submit_one()
        self.assertEqual(self.row('legacy.raw')['state'], 'review')
        self.assertEqual(self.sent, [])

    def test_unparseable_legacy_job_stops_worker(self):
        (self.spool / 'processing' / 'legacy.raw').write_bytes(b'raw')
        with self.assertRaises(RuntimeError):
            self.queue.recover(lambda raw: None)

    def test_terminal_state_recovers_archive_after_crash(self):
        self.add_job()
        self.queue.update('a.raw', state='completed')
        self.queue.recover(lambda raw: '9035263662')
        self.assertTrue((self.spool / 'completed' / 'a.raw').exists())

    def test_pinned_pdf_survives_source_cleanup(self):
        self.add_job()
        (self.spool / 'source.pdf').unlink()
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)

    def test_lock_excludes_second_process(self):
        with acquire_lock(self.spool):
            with self.assertRaises(RuntimeError):
                acquire_lock(self.spool)
        with acquire_lock(self.spool):
            pass

    def test_payload_validation_and_sdk_objects(self):
        data = self.result('Sent')
        self.assertEqual(delivery_result(data, '+19035263662'), ('Sent', None))
        data['to'][0] = SimpleNamespace(**data['to'][0])
        self.assertEqual(delivery_result(SimpleNamespace(**data), '+19035263662'), ('Sent', None))
        self.assertEqual(delivery_result(data, '+15126286857'), ('Unknown', None))
        data = self.result('SendingFailed', 'LineBusy')
        data['to'][0]['messageStatus'] = 'Sent'
        self.assertEqual(delivery_result(data, '+19035263662'), ('Unknown', None))
        self.assertEqual(delivery_result({}, '+19035263662'), ('Unknown', None))

    def test_normalization(self):
        for number in ('9035263662', '1 (903) 526-3662', '+19035263662'):
            self.assertEqual(normalize_number(number), '+19035263662')
        for number in (None, '', '9035263662 ext 5'):
            with self.assertRaises(ValueError):
                normalize_number(number)

    def test_queued_timeout_at_epoch_zero_is_held_without_resend(self):
        self.now = 0
        self.add_job()
        self.queue.submit_one()
        self.now = 1800
        self.responses['1'] = self.result('Queued')
        self.queue.poll_one()
        self.assertEqual(self.row()['state'], 'review')
        self.queue.submit_one()
        self.assertEqual(len(self.sent), 1)


if __name__ == '__main__':
    unittest.main()
