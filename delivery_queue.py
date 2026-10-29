"""Durable fax delivery scheduling. No RingCentral SDK dependency.

Only final SendingFailed/LineBusy responses are automatically resubmitted.
An interrupted or ambiguous POST is held for reconciliation, never replayed.
"""

import argparse
import fcntl
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path


class RetryLater(Exception):
    """A confirmed API rejection or failed GET; no delivery decision was made."""

    def __init__(self, delay, message):
        super().__init__(message)
        self.delay = delay


def normalize_number(number):
    text = str(number or '').strip()
    if not text or re.search(r'[^+\d\s().-]', text):
        raise ValueError('Missing or unsupported fax number')
    digits = re.sub(r'\D', '', text)
    if len(digits) == 10 and not text.startswith('+'):
        digits = '1' + digits
    if not 8 <= len(digits) <= 15:
        raise ValueError('Invalid fax number length')
    return '+' + digits


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def delivery_result(data, recipient):
    """Require the expected single recipient; do not confuse aggregate status.

    RingCentral's Message Store schema defines to[].faxErrorCode=LineBusy.
    https://developers.ringcentral.com/api-reference/Message-Store/readMessage
    """
    recipients = value(data, 'to', []) or []
    if len(recipients) != 1:
        return 'Unknown', None
    target = recipients[0]
    try:
        if normalize_number(value(target, 'phoneNumber')) != recipient:
            return 'Unknown', None
    except ValueError:
        return 'Unknown', None
    aggregate = value(data, 'messageStatus')
    status = value(target, 'messageStatus') or aggregate
    # Conflicting aggregate/recipient states are not a reason to send again.
    if aggregate and status != aggregate:
        return 'Unknown', None
    return status, value(target, 'faxErrorCode')


def acquire_lock(spool):
    """Hold this file handle for the entire worker/admin process lifetime."""
    handle = (Path(spool) / 'worker.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('Another fax worker/admin process holds worker.lock')
    return handle


class DeliveryQueue:
    def __init__(self, spool, submit, fetch, log=print, clock=time.time,
                 busy_delay=120, max_busy_retries=5, poll_interval=30,
                 review_after=1800, send_interval=7):
        self.spool = Path(spool)
        self.submit, self.fetch, self.log, self.clock = submit, fetch, log, clock
        self.busy_delay, self.max_busy_retries = busy_delay, max_busy_retries
        self.poll_interval, self.review_after = poll_interval, review_after
        self.send_interval = send_interval
        self.state = self.spool / 'delivery-state'
        self.state.mkdir(exist_ok=True)
        self.documents = self.state / 'documents'
        self.documents.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.state / 'queue.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS jobs (
                job TEXT PRIMARY KEY, recipient TEXT NOT NULL, pdf TEXT,
                state TEXT NOT NULL, message_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0, submitted REAL, next_poll REAL NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS attempts (
                job TEXT NOT NULL, attempt INTEGER NOT NULL, message_id TEXT,
                submitted REAL NOT NULL, result TEXT,
                PRIMARY KEY(job, attempt)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL);
        ''')

    def close(self):
        self.db.close()

    def rows(self):
        return self.db.execute('SELECT rowid, * FROM jobs ORDER BY rowid').fetchall()

    def update(self, job, **fields):
        # All column names come from this module, not external input.
        with self.db:
            self.db.execute('UPDATE jobs SET ' + ', '.join(k + '=?' for k in fields)
                            + ' WHERE job=?', [*fields.values(), job])

    def pause_until(self, key):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else 0

    def pause(self, key, until):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, until))

    def register(self, raw, recipient, pdf=None, review=False):
        recipient = normalize_number(recipient)
        pinned = None
        if pdf is not None:
            # Keep active/retrying documents outside the artifacts/ cleanup policy.
            pinned = self.documents / (raw.name + '.pdf')
            shutil.copyfile(pdf, pinned)
        with self.db:
            self.db.execute('INSERT INTO jobs(job,recipient,pdf,state,detail) VALUES(?,?,?,?,?)',
                            (raw.name, recipient, str(pinned) if pinned else None,
                             'review' if review else 'ready',
                             'Untracked processing file; reconcile before sending' if review else ''))

    def recover(self, extract_recipient):
        # POST may have reached RingCentral before a crash, even without an ID.
        with self.db:
            self.db.execute("UPDATE jobs SET state='review', detail=? WHERE state='submitting'",
                            ('Interrupted submission; reconcile in RingCentral before retrying',))
        known = {row['job'] for row in self.rows()}
        for raw in sorted((self.spool / 'processing').iterdir()):
            if raw.is_file() and raw.name not in known:
                try:
                    self.register(raw, extract_recipient(raw), review=True)
                except Exception as exc:
                    # Unknown destination means we cannot safely enforce its lock.
                    raise RuntimeError(f'Cannot reconcile legacy processing file {raw}: {exc}') from exc
        self.archive_finished()
        for row in self.rows():
            if row['state'] == 'review':
                self.log(f"REVIEW {row['job']} -> {row['recipient']}: {row['detail']}")

    def archive_finished(self):
        for row in self.rows():
            if row['state'] not in ('completed', 'failed'):
                continue
            source = self.spool / 'processing' / row['job']
            destination = self.spool / row['state'] / row['job']
            if source.exists():
                if destination.exists():
                    raise RuntimeError(f'Archive collision: {destination}')
                source.replace(destination)
            if row['pdf']:
                Path(row['pdf']).unlink(missing_ok=True)

    def poll_one(self):
        now = self.clock()
        if now < self.pause_until('api_pause'):
            return
        row = self.db.execute("""SELECT * FROM jobs
            WHERE state IN ('active','review') AND message_id IS NOT NULL AND next_poll<=?
            ORDER BY next_poll, rowid LIMIT 1""", (now,)).fetchone()
        if row is None:
            return
        self.update(row['job'], next_poll=now + self.poll_interval)
        try:
            data = self.fetch(row['message_id'])
        except RetryLater as exc:
            self.pause('api_pause', now + exc.delay)
            self.log(f"Status check deferred: {exc}")
            return
        except Exception as exc:
            self.update(row['job'], detail=f'Status unavailable: {exc}')
            if row['submitted'] is not None and now - row['submitted'] >= self.review_after:
                self.update(row['job'], state='review')
            self.log(f"Status unavailable for {row['job']}; keeping destination blocked: {exc}")
            return
        status, error = delivery_result(data, row['recipient'])
        detail = f'{status or "Unknown"}' + (f' / {error}' if error else '')
        with self.db:
            self.db.execute('UPDATE attempts SET result=? WHERE job=? AND attempt=?',
                            (detail, row['job'], row['attempts']))
        if status == 'Sent':
            self.update(row['job'], state='completed', detail=detail)
            self.log(f"DELIVERED {row['job']} -> {row['recipient']}")
        elif status == 'SendingFailed' and error == 'LineBusy':
            if row['attempts'] <= self.max_busy_retries:
                self.update(row['job'], state='retry', due=now + self.busy_delay,
                            message_id=None, detail=detail)
                self.log(f"BUSY {row['job']}; retry in {self.busy_delay}s "
                         f"({row['attempts']}/{self.max_busy_retries} retries)")
            else:
                self.update(row['job'], state='failed', detail='Busy retry limit reached: ' + detail)
                self.log(f"FAILED {row['job']}: busy retry limit reached")
        elif status == 'SendingFailed' and error and error not in ('Undefined', 'SentPartially'):
            self.update(row['job'], state='failed', detail=detail)
            self.log(f"FAILED {row['job']}: {detail}; no automatic resend")
        else:
            # Missing/unknown/partial outcomes block this destination and remain
            # polled when there is an ID, allowing late confirmation to resolve it.
            submitted = row['submitted'] if row['submitted'] is not None else now
            review = status != 'Queued' or now - submitted >= self.review_after
            self.update(row['job'], state='review' if review else 'active', detail=detail)
            if review and (row['state'] != 'review' or row['detail'] != detail):
                self.log(f"REVIEW {row['job']}: {detail}; no automatic resend")
        self.archive_finished()

    def submit_one(self):
        now = self.clock()
        if now < max(self.pause_until('api_pause'), self.pause_until('next_send')):
            return
        # The oldest unfinished job owns its normalized destination, including
        # while waiting for a retry or manual review. Other destinations proceed.
        row = self.db.execute("""SELECT a.* FROM jobs a
            WHERE a.state IN ('ready','retry') AND a.due<=?
            AND NOT EXISTS (SELECT 1 FROM jobs b WHERE b.recipient=a.recipient
                AND b.state NOT IN ('completed','failed') AND b.rowid<a.rowid)
            ORDER BY a.rowid LIMIT 1""", (now,)).fetchone()
        if row is None:
            return
        attempt = row['attempts'] + 1
        # Commit intent BEFORE POST. A crash after here is held, not replayed.
        with self.db:
            self.db.execute("UPDATE jobs SET state='submitting', attempts=?, submitted=? WHERE job=?",
                            (attempt, now, row['job']))
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?)',
                            (row['job'], attempt, None, now, 'Submitting'))
        self.pause('next_send', now + self.send_interval)
        try:
            message_id = self.submit(row['recipient'], row['pdf'])
            if not message_id or not str(message_id).isdigit():
                raise RuntimeError('Submission returned no usable message ID')
        except RetryLater as exc:
            # Confirmed 401/429: request was rejected, safe to retry submission.
            with self.db:
                self.db.execute('DELETE FROM attempts WHERE job=? AND attempt=?', (row['job'], attempt))
                self.db.execute("UPDATE jobs SET state=?, attempts=?, due=?, detail=? WHERE job=?",
                                (row['state'], row['attempts'], now + exc.delay, str(exc), row['job']))
            self.pause('api_pause', now + exc.delay)
            self.log(f"Submission deferred for {row['job']}: {exc}")
            return
        except Exception as exc:
            self.update(row['job'], state='review', detail=f'Submission uncertain: {exc}')
            self.log(f"REVIEW {row['job']}: submission uncertain; do not resend automatically: {exc}")
            return
        with self.db:
            self.db.execute("UPDATE jobs SET state='active', message_id=?, next_poll=?, detail='Queued' WHERE job=?",
                            (str(message_id), now + self.poll_interval, row['job']))
            self.db.execute('UPDATE attempts SET message_id=?, result=? WHERE job=? AND attempt=?',
                            (str(message_id), 'Queued', row['job'], attempt))
        self.log(f"SUBMITTED {row['job']} -> {row['recipient']}; message {message_id}; awaiting delivery")


def main():
    parser = argparse.ArgumentParser(description='Inspect/reconcile durable fax delivery jobs. Stop worker before use.')
    parser.add_argument('--spool', type=Path, default=Path('/var/spool/ringcentral-fax'))
    parser.add_argument('--job', help='Exact queued filename to reconcile')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--message-id', help='Attach the verified RingCentral message ID; resume monitoring only')
    actions.add_argument('--confirmed-sent', action='store_true', help='Archive after manually verifying delivery')
    actions.add_argument('--confirmed-failed', action='store_true', help='Archive after verifying no transmission remains active')
    args = parser.parse_args()
    if bool(args.job) != bool(args.message_id or args.confirmed_sent or args.confirmed_failed):
        parser.error('--job and exactly one reconciliation action must be provided together')
    if args.message_id and not args.message_id.isdigit():
        parser.error('--message-id must be numeric')
    with acquire_lock(args.spool):
        queue = DeliveryQueue(args.spool, None, None)
        try:
            if args.job:
                row = queue.db.execute('SELECT * FROM jobs WHERE job=?', (args.job,)).fetchone()
                if row is None or row['state'] not in ('review', 'submitting'):
                    parser.error('Only review/submitting jobs can be manually reconciled')
                if args.message_id:
                    queue.update(args.job, state='active', message_id=args.message_id,
                                 submitted=time.time(), next_poll=0, detail='Manually reconciled message ID')
                else:
                    queue.update(args.job, state='completed' if args.confirmed_sent else 'failed',
                                 detail='Manually confirmed sent' if args.confirmed_sent else 'Manually confirmed failed')
                    queue.archive_finished()
            print(json.dumps([dict(row) for row in queue.rows()], indent=2))
        finally:
            queue.close()


if __name__ == '__main__':
    main()
