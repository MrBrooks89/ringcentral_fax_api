#!/usr/bin/env python3

import argparse
import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from ringcentral import SDK

from process_print_job import process_job


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

MAX_SEND_ATTEMPTS = 3


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


def is_rate_limit_error(exc):
    """
    Detect known RingCentral rate-limit indicators.
    """

    text = str(exc).lower()

    indicators = [
        "429",
        "cmn-301",
        "rate limit",
        "request rate exceeded",
        "too many requests",
    ]

    return any(indicator in text for indicator in indicators)


def is_auth_expiration_error(exc):
    """
    Detect an expired RingCentral OAuth session.

    The JWT credential remains available, so the worker can use it
    to establish a completely fresh OAuth session automatically.
    """

    text = str(exc).lower()

    indicators = [
        "refresh token has expired",
        "access token has expired",
        "token is expired",
        "token expired",
        "unauthorized",
        "401",
    ]

    return any(indicator in text for indicator in indicators)


def get_retry_after(exc):
    """
    Try to retrieve Retry-After from the SDK exception.

    Fall back to 65 seconds, slightly longer than the observed
    RingCentral 60-second penalty interval.
    """

    possible_responses = [
        getattr(exc, "response", None),
        getattr(exc, "resp", None),
    ]

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


def send_with_retry(
    sdk,
    platform,
    recipient,
    fax_pdf,
):
    """
    Submit a fax with handling for:

    - expired OAuth access/refresh sessions
    - RingCentral HTTP 429 rate limiting
    - transient API failures

    Authentication expiration is handled specially. The worker
    re-authenticates using the JWT and retries without requiring
    a service restart.
    """

    reauthenticated = False

    for attempt in range(
        1,
        MAX_SEND_ATTEMPTS + 1,
    ):
        try:
            log(f"Send attempt {attempt}/{MAX_SEND_ATTEMPTS} for {recipient}")

            return send_fax(
                sdk,
                platform,
                recipient,
                fax_pdf,
            )

        except Exception as exc:
            #
            # ----------------------------------------------------
            # EXPIRED RINGCENTRAL OAUTH SESSION
            # ----------------------------------------------------
            #
            # The worker may remain running longer than the
            # RingCentral refresh-token lifetime.
            #
            # Re-authenticate with the JWT and retry using the
            # newly-created OAuth session.
            #

            if is_auth_expiration_error(exc):
                if reauthenticated:
                    log(
                        "RingCentral authentication still failed "
                        "after JWT re-authentication."
                    )

                    raise

                try:
                    reauthenticate_with_jwt(platform)

                except Exception as auth_exc:
                    log(f"RingCentral JWT re-authentication failed: {auth_exc}")

                    raise

                reauthenticated = True

                #
                # Immediately retry the fax using the new
                # authenticated session.
                #
                continue

            #
            # ----------------------------------------------------
            # RINGCENTRAL RATE LIMIT
            # ----------------------------------------------------
            #

            if is_rate_limit_error(exc):
                wait_time = get_retry_after(exc)

                log(
                    "RingCentral rate limit encountered. "
                    f"Waiting {wait_time} seconds "
                    "before retry."
                )

                time.sleep(wait_time)

                continue

            #
            # ----------------------------------------------------
            # OTHER API / NETWORK FAILURE
            # ----------------------------------------------------
            #

            log(f"Fax send attempt {attempt} failed: {exc}")

            if attempt >= MAX_SEND_ATTEMPTS:
                raise

            time.sleep(10)

    raise RuntimeError("Fax failed after maximum retry attempts.")


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

    pending_file.replace(processing_file)

    return processing_file


def move_completed(processing_file):
    """
    Move a successfully processed original SAP job into completed/.
    """

    destination = COMPLETED_DIR / processing_file.name

    shutil.move(
        str(processing_file),
        str(destination),
    )

    return destination


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


def process_queue_job(
    processing_file,
    sdk=None,
    platform=None,
    send=False,
):
    """
    Process one claimed SAP job.

    process_job(..., send=False) performs only the PCL/PDF work.
    RingCentral submission is handled here so the persistent SDK
    session can be reused between jobs.
    """

    log("")
    log("=" * 60)
    log(f"Processing: {processing_file}")
    log("=" * 60)

    raw_data = processing_file.read_bytes()

    #
    # Convert SAP PCL to the final fax PDF.
    #
    # send=False is intentional: we do not want
    # process_print_job.py launching send_fax.py because that
    # would authenticate separately for every job.
    #

    result = process_job(
        raw_data,
        send=False,
    )

    fax = result["fax"]
    fax_pdf = result["fax_pdf"]

    #
    # Worker dry-run mode.
    #

    if not send:
        log("")
        log("WORKER DRY RUN SUCCESS")
        log(f"Fax:      {fax}")
        log(f"Fax PDF:  {fax_pdf}")

        return result

    #
    # Persistent RingCentral submission.
    #

    message_id = send_with_retry(
        sdk,
        platform,
        fax,
        fax_pdf,
    )

    result["message_id"] = message_id

    return result


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
        help=("Process one pending job and exit. Useful for testing."),
    )

    parser.add_argument(
        "--poll-interval",
        type=int,
        default=2,
        help=("Seconds to wait when the queue is empty."),
    )

    return parser.parse_args()


#
# ============================================================
# MAIN WORKER LOOP
# ============================================================
#


def main():
    args = parse_args()

    #
    # Ensure queue directories exist.
    #

    for directory in [
        PENDING_DIR,
        PROCESSING_DIR,
        COMPLETED_DIR,
        FAILED_DIR,
    ]:
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

    sdk = None
    platform = None

    #
    # Production/send mode authenticates once at worker startup.
    #

    if args.send:
        try:
            sdk, platform = create_platform()

        except Exception as exc:
            log(f"ERROR: Unable to initialize RingCentral: {exc}")

            return 1

    else:
        log("Worker running in DRY-RUN mode. No faxes will be submitted.")

    #
    # Persistent worker loop.
    #

    while True:
        pending_file = get_next_pending_job()

        #
        # Queue empty.
        #

        if pending_file is None:
            if args.once:
                log("No pending jobs.")

                return 0

            time.sleep(args.poll_interval)

            continue

        #
        # Claim the job.
        #

        try:
            processing_file = claim_job(pending_file)

        except FileNotFoundError:
            #
            # Another process may have moved the file between
            # discovery and claim. Just check the queue again.
            #
            continue

        #
        # Process and optionally transmit.
        #

        try:
            process_queue_job(
                processing_file,
                sdk=sdk,
                platform=platform,
                send=args.send,
            )

            completed_file = move_completed(processing_file)

            log(f"Job completed: {completed_file}")

            #
            # Pace outbound fax submissions.
            #

            if args.send:
                log(f"Pacing next fax for {SEND_INTERVAL} seconds...")

                time.sleep(SEND_INTERVAL)

        except Exception as exc:
            log(f"ERROR processing {processing_file}: {exc}")

            try:
                failed_file = move_failed(processing_file)

                log(f"Job moved to failed queue: {failed_file}")

            except Exception as move_exc:
                log(f"CRITICAL: Could not move failed job: {move_exc}")

        #
        # --once processes exactly one job.
        #

        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
