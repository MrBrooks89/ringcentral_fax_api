# Delivery tracking and busy retries

The production worker retains jobs in `processing/` until RingCentral confirms
`Sent` through the Message Store. An accepted fax POST is only a submission,
not proof of delivery. SAP/CUPS ingestion is unchanged.

## Scheduling and retry policy

- One unfinished job owns each normalized destination number. Formatting variants
  such as `(903) 526-3662` and `+19035263662` share the same destination queue.
- Other numbers can be submitted while a destination is receiving, retrying, or
  awaiting manual review. The existing seven-second global POST pacing remains.
- Only a **final `SendingFailed` with recipient `faxErrorCode: LineBusy`** is
  retried automatically. A `Queued` message is still owned by RingCentral; do not
  submit a second copy while RingCentral may be transmitting or retrying it.
- Default: wait 120 seconds, with five additional submissions after the initial
  submission. Exhausted jobs move to `failed/` and release the destination.
- Definite non-busy failures move to `failed/` without automatic resubmission.
  Partial sends, missing error details, unknown statuses, conflicting recipients,
  and ambiguous submissions are held for review. Review holds block that number.
- Status checks default to 30 seconds per message; the worker services one due
  check per scheduler cycle. Under load the interval can be longer. HTTP 429
  pauses both status checks and submissions using `Retry-After` (65s fallback).
- Explicit HTTP 401 causes JWT re-authentication and a deferred retry. Network
  errors, 5xx, missing message IDs, or interruption during a POST are ambiguous:
  the worker **does not automatically repeat that POST**.
- After 30 minutes without a clear outcome, a job is marked for review. Known
  message IDs continue to be monitored, so late delivery confirmation can resolve
  the hold. This threshold never causes automatic resubmission.

RingCentral schema: [Message Store](https://developers.ringcentral.com/api-reference/Message-Store/readMessage).
The busy code is `LineBusy`, not the call-log display label `Busy`.

## Persistent files

`delivery-state/queue.sqlite3` stores job state, message IDs, submission history,
retry counts, due times, and API pacing. `delivery-state/documents/` holds copies
of PDFs needed by active/retrying jobs, outside the seven-day `artifacts/`
cleanup policy. Pinned PDFs are removed after completion or final failure.

Back up this directory together with `processing/` while the worker is stopped.
Do not delete or age-clean active state. Database records are retained for
reconciliation; monitor disk usage. A process lock prevents two production,
one-shot, dry-run, or administration processes from running simultaneously.
Standalone `send_fax.py` and direct RingCentral/app submissions bypass this queue;
avoid those when testing per-number serialization.

Untracked files already in `processing/` at upgrade are held for review, because
the old worker may have submitted them before stopping. Files already in
`completed/` are not retroactively checked. If an old processing file has no
readable destination metadata, startup stops for administrator investigation.

## Configuration

Optional entries in `/opt/ringcentral-fax/.env`:

```dotenv
FAX_BUSY_RETRY_SECONDS=120
FAX_MAX_BUSY_RETRIES=5
FAX_STATUS_POLL_SECONDS=30
FAX_REVIEW_AFTER_SECONDS=1800
```

`FAX_MAX_BUSY_RETRIES=0` disables automatic busy resubmissions. Minimum values:
retry delay 1s, status polling 10s, review threshold 60s. Restart to apply changes.
The authenticated RingCentral app/user needs **ReadMessages** in addition to its
existing fax sending access. Verify Message Store reads before a production batch.

## Upgrade an existing installation

Run the offline tests from the reviewed repository checkout:

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

During a maintenance window, stop the worker before replacing files. Existing
queued inputs stay in the spool. Back up `/opt/ringcentral-fax` and the entire
spool using your normal backup process while the service is stopped.

```bash
sudo systemctl stop ringcentral-fax-worker
sudo install -o root -g lp -m 750 fax_worker.py delivery_queue.py /opt/ringcentral-fax/
sudo install -o root -g root -m 644 ringcentral-fax.conf /etc/tmpfiles.d/ringcentral-fax.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/ringcentral-fax.conf
```

The existing Python environment and service unit can be reused; SQLite is part
of Python's standard library. Do not rerun the full OS/GhostPDL installer merely
to upgrade these two modules. The installer also includes the module for new installs.

Check old `processing/` files and review holds before restarting bulk traffic.
Then start the worker and monitor a controlled test fax:

```bash
sudo systemctl start ringcentral-fax-worker
sudo journalctl -u ringcentral-fax-worker -f
```

Verify `SUBMITTED` followed by `DELIVERED`, then two jobs to the same test number
one at a time. Other destinations should continue independently. Confirm a busy
test is reported as `SendingFailed / LineBusy` before enabling a large retry batch.
No live faxes are sent by the automated tests.

## Inspect and reconcile held jobs

Stop the worker before running the administration CLI (it takes the same lock):

```bash
sudo systemctl stop ringcentral-fax-worker
sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/delivery_queue.py
```

This prints job filenames, state, destination, message ID, attempts, and detail.
For an ambiguous submission, first find the matching job in RingCentral using
destination, submission time, and document identity. Do not identify a job from
the destination alone when many orders were queued.

Attach a verified message ID to resume **monitoring**, not submission:

```bash
sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/delivery_queue.py \
  --job EXACT_JOB_FILENAME --message-id VERIFIED_NUMERIC_MESSAGE_ID
```

Alternatively, after confirming delivery or confirming a failed/cancelled job
has no transmission or retries left in RingCentral:

```bash
sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/delivery_queue.py \
  --job EXACT_JOB_FILENAME --confirmed-sent
# OR, only after verifying it is safe to release the destination:
sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/delivery_queue.py \
  --job EXACT_JOB_FILENAME --confirmed-failed
sudo systemctl start ringcentral-fax-worker
```

Untracked legacy jobs have no pinned PDF, so attaching an ID can confirm delivery
but cannot make a usable automatic resend. If a legacy job must be sent again,
confirm the original attempt is finished, mark it failed, then resubmit through
SAP as a **new job with a new filename**. Do not copy a tracked filename back to
pending or delete database records to force a resend.

`fax_worker.py --once --send` now runs one scheduler cycle and saves outstanding
delivery state. Run the service to continue monitoring. A dry run converts one
job and returns its original to `pending/`; it does not authenticate, send, or
claim that delivery completed. Stop the service before either diagnostic command.

For rollback, stop the worker and reconcile all active/review jobs before using
the old submission-only worker; the old version does not honor destination holds.
