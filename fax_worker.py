#!/usr/bin/env python3

import argparse
import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from ringcentral import SDK

from process_print_job import process_job, extract_metadata
from delivery_queue import DeliveryQueue, RetryLater, acquire_lock


APP_DIR = Path("/opt/ringcentral-fax")
SPOOL_DIR = Path("/var/spool/ringcentral-fax")

PENDING_DIR = SPOOL_DIR / "pending"
PROCESSING_DIR = SPOOL_DIR / "processing"
COMPLETED_DIR = SPOOL_DIR / "completed"
FAILED_DIR = SPOOL_DIR / "failed"

# Pace outbound fax submissions so normal bulk jobs stay below
# the observed RingCentral Heavy API limit.
SEND_INTERVAL = 7

# Fallback if RingCentral returns a rate limit but the SDK does
# not expose a usable Retry-After header.
DEFAULT_RATE_LIMIT_WAIT = 65



#
# ============================================================
# LOAD RINGCENTRAL CONFIGURATION
# ============================================================
#

load_dotenv(APP_DIR / ".env")

CLIENT_ID = os.getenv("RC_CLIENT_ID")
CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET")
JWT_TOKEN = os.getenv("RC_JWT_TOKEN")
SERVER_URL = os.getenv("RC_SERVER")


def log(message):
    """
    Print immediately so messages appear in journalctl without
    Python output buffering delaying them.
    """

    print(message, flush=True)


def validate_environment():
    """
    Make sure all required RingCentral settings are available.
    """

    required = {
        "RC_CLIENT_ID": CLIENT_ID,
        "RC_CLIENT_SECRET": CLIENT_SECRET,
        "RC_JWT_TOKEN": JWT_TOKEN,
        "RC_SERVER": SERVER_URL,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


#
# ============================================================
# RINGCENTRAL SESSION
# ============================================================
#


def create_platform():
    """
    Create the RingCentral SDK/platform and authenticate once
    using the configured JWT credential.

    The returned platform object is reused for all queued faxes.
    """

    validate_environment()

    log("Creating RingCentral SDK session...")

    sdk = SDK(
        CLIENT_ID,
        CLIENT_SECRET,
        SERVER_URL,
    )

    platform = sdk.platform()

    log("Authenticating to RingCentral...")

    platform.login(jwt=JWT_TOKEN)

    log("RingCentral authentication successful.")

    return sdk, platform


def reauthenticate_with_jwt(platform):
    """
    Establish a fresh RingCentral OAuth session using the JWT.

    This is used when the long-running worker discovers that its
    access/refresh-token session has expired.
    """

    log("RingCentral OAuth session expired. Re-authenticating with JWT...")

    platform.login(jwt=JWT_TOKEN)

    log("RingCentral JWT re-authentication successful.")


def get_value(obj, name, default=None):
    """
    RingCentral SDK responses may behave like either dictionaries
    or objects depending on SDK/version.
    """

    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


#
# ============================================================
# ERROR CLASSIFICATION
# ============================================================
#


def get_retry_after(exc):
    """
    Try to retrieve Retry-After from the SDK exception.

    Fall back to 65 seconds, slightly longer than the observed
    RingCentral 60-second penalty interval.
    """

    possible_responses = response_chain(exc)

    for response in possible_responses:
        if response is None:
            continue

        headers = getattr(response, "headers", None)

        if not headers:
            continue

        try:
            value = headers.get("Retry-After")

            if value:
                return max(int(value), 1)

        except (TypeError, ValueError):
            pass

    return DEFAULT_RATE_LIMIT_WAIT


#
# ============================================================
# RINGCENTRAL FAX SUBMISSION
# ============================================================
#


def send_fax(
    sdk,
    platform,
    recipient,
    filename,
    resolution="High",
    cover_index=0,
):
    """
    Submit one fax using the already-authenticated persistent
    RingCentral platform session.
    """

    body = {
        "to": [{"phoneNumber": recipient}],
        "faxResolution": resolution,
        "coverIndex": cover_index,
    }

    builder = sdk.create_multipart_builder()

    builder.set_body(body)

    with open(filename, "rb") as file_handle:
        content = file_handle.read()

    builder.add(
        (
            Path(filename).name,
            content,
        )
    )

    request = builder.request("/restapi/v1.0/account/~/extension/~/fax")

    response = platform.send_request(request)

    data = response.json()

    message_id = get_value(
        data,
        "id",
    )

    status = get_value(
        data,
        "messageStatus",
    )

    if not message_id:
        raise RuntimeError(
            "RingCentral accepted the request but no message ID was returned."
        )

    log("Fax submitted successfully.")
    log(f"To:         {recipient}")
    log(f"Message ID: {message_id}")
    log(f"Status:     {status}")

    return message_id


def response_chain(exc):
    """Walk public SDK exception/response accessors (including api_response())."""
    candidates = [exc]
    for _ in range(3):
        next_candidates = []
        for candidate in candidates:
            yield candidate
            for name in ("response", "resp", "api_response"):
                child = getattr(candidate, name, None)
                if callable(child):
                    try:
                        child = child()
                    except Exception:
                        continue
                if child is not None:
                    next_candidates.append(child)
        candidates = next_candidates


def http_status(exc):
    for candidate in response_chain(exc):
        status = getattr(candidate, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def safe_api_error(exc):
    # SDK exception text can contain request headers, including credentials.
    return RuntimeError(f"{type(exc).__name__} (HTTP {http_status(exc) or 'unknown'})")


def defer_rejected_request(exc, platform):
    """Only explicit HTTP rejections are safe to submit again."""
    status = http_status(exc)
    # The SDK may try to refresh an expired session before sending the fax.
    # A rejected OAuth refresh is also known not to have submitted the fax.
    oauth_rejected = False
    if status == 400:
        for response in response_chain(exc):
            request = getattr(response, "request", None)
            if callable(request):
                request = request()
            url = getattr(request, "url", "") or ""
            if url.split("?", 1)[0].endswith("/restapi/oauth/token"):
                oauth_rejected = True
    if status == 401 or oauth_rejected:
        try:
            reauthenticate_with_jwt(platform)
        except Exception as auth_exc:
            raise safe_api_error(auth_exc) from None
        raise RetryLater(10, "OAuth session refreshed after authentication rejection") from exc
    if status == 429:
        raise RetryLater(get_retry_after(exc), "RingCentral HTTP 429") from exc


def send_with_retry(sdk, platform, recipient, fax_pdf):
    """Submit once. The durable scheduler handles confirmed 401/429 rejections.

    Timeouts, network failures, and 5xx responses are ambiguous: they must
    never trigger another POST automatically.
    """
    try:
        return send_fax(sdk, platform, recipient, fax_pdf)
    except Exception as exc:
        defer_rejected_request(exc, platform)
        raise safe_api_error(exc) from None


def fetch_delivery(platform, message_id):
    try:
        return platform.get(
            f"/restapi/v1.0/account/~/extension/~/message-store/{message_id}"
        ).json()
    except Exception as exc:
        defer_rejected_request(exc, platform)
        raise safe_api_error(exc) from None



#
# ============================================================
# QUEUE HANDLING
# ============================================================
#


def get_next_pending_job():
    """
    Return the oldest/sorted pending file.

    Only regular files are considered.
    """

    jobs = sorted(path for path in PENDING_DIR.iterdir() if path.is_file())

    if not jobs:
        return None

    return jobs[0]


def claim_job(pending_file):
    """
    Atomically move a pending job into processing.

    Because pending/ and processing/ are on the same filesystem,
    Path.replace() prevents the same job from being claimed twice.
    """

    processing_file = PROCESSING_DIR / pending_file.name

    if processing_file.exists():
        raise RuntimeError(f"Refusing to overwrite processing job: {processing_file}")
    pending_file.replace(processing_file)

    return processing_file


def move_failed(processing_file):
    """
    Move a failed original SAP job into failed/ for investigation
    and possible controlled retry.
    """

    destination = FAILED_DIR / processing_file.name

    shutil.move(
        str(processing_file),
        str(destination),
    )

    return destination


#
# ============================================================
# JOB PROCESSING
# ============================================================
#


def prepare_queue_job(processing_file):
    """Convert the SAP job without submitting; all POSTs use DeliveryQueue."""
    log(f"Converting: {processing_file}")
    return process_job(processing_file.read_bytes(), send=False)


#
# ============================================================
# COMMAND-LINE OPTIONS
# ============================================================
#


def parse_args():
    parser = argparse.ArgumentParser(
        description=("Persistent RingCentral fax queue worker.")
    )

    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "Actually submit faxes to RingCentral. "
            "Without this option the worker performs "
            "dry runs."
        ),
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=("Run one scheduler cycle and exit; delivery monitoring resumes on restart."),
    )

    parser.add_argument(
        "--poll-interval",
        type=int,
        default=2,
        help=("Seconds between scheduler cycles (minimum effective interval: 2)."),
    )

    return parser.parse_args()


#
# ============================================================
# MAIN WORKER LOOP
# ============================================================
#


def main():
    args = parse_args()
    if args.poll_interval < 1:
        raise ValueError("--poll-interval must be at least 1 second")
    for directory in [PENDING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

    # Prevent a second worker (including --once/dry-run) bypassing destination locks.
    with acquire_lock(SPOOL_DIR):
        if not args.send:
            # Dry runs remain conversion-only, never mark them as delivered.
            pending = get_next_pending_job()
            if pending is None:
                log("No pending jobs.")
                return 0
            processing = claim_job(pending)
            try:
                prepare_queue_job(processing)
            finally:
                processing.replace(PENDING_DIR / processing.name)
            log("Dry run finished; original job remains pending.")
            return 0

        try:
            sdk, platform = create_platform()
        except Exception as exc:
            log(f"Unable to initialize RingCentral: {safe_api_error(exc)}")
            return 1
        queue = DeliveryQueue(
            SPOOL_DIR,
            submit=lambda recipient, pdf: send_with_retry(sdk, platform, recipient, pdf),
            fetch=lambda message_id: fetch_delivery(platform, message_id),
            log=log,
            busy_delay=env_int("FAX_BUSY_RETRY_SECONDS", 120, minimum=1),
            max_busy_retries=env_int("FAX_MAX_BUSY_RETRIES", 5, minimum=0),
            poll_interval=env_int("FAX_STATUS_POLL_SECONDS", 30, minimum=10),
            review_after=env_int("FAX_REVIEW_AFTER_SECONDS", 1800, minimum=60),
            send_interval=SEND_INTERVAL,
        )
        try:
            queue.recover(lambda raw: extract_metadata(raw.read_bytes())["fax"])
            while True:
                # Register one new input each turn; the scheduler can then skip
                # a blocked destination while continuing to other numbers.
                pending = get_next_pending_job()
                if pending is not None:
                    processing = claim_job(pending)
                    try:
                        result = prepare_queue_job(processing)
                        queue.register(processing, result["fax"], result["fax_pdf"])
                    except Exception as exc:
                        log(f"Conversion/registration failed for {processing}: {exc}")
                        # If registration committed, preserve state and stop:
                        # never archive a job that might now be tracked.
                        if any(row["job"] == processing.name for row in queue.rows()):
                            raise
                        move_failed(processing)
                queue.archive_finished()
                queue.poll_one()
                queue.submit_one()
                if args.once:
                    log("One scheduler cycle complete; outstanding delivery state is saved.")
                    return 0
                time.sleep(max(args.poll_interval, 2))
        finally:
            queue.close()


def env_int(name, default, minimum):
    result = int(os.getenv(name, str(default)))
    if result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
