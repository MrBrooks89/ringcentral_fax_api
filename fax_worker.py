#!/usr/bin/env python3

import argparse
import os
import shutil
import sys
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

SEND_INTERVAL = 7
DEFAULT_RATE_LIMIT_WAIT = 65
MAX_SEND_ATTEMPTS = 3


load_dotenv(APP_DIR / ".env")

CLIENT_ID = os.getenv("RC_CLIENT_ID")
CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET")
JWT_TOKEN = os.getenv("RC_JWT_TOKEN")
SERVER_URL = os.getenv("RC_SERVER")


def log(message):
    print(message, flush=True)


def validate_environment():
    required = {
        "RC_CLIENT_ID": CLIENT_ID,
        "RC_CLIENT_SECRET": CLIENT_SECRET,
        "RC_JWT_TOKEN": JWT_TOKEN,
        "RC_SERVER": SERVER_URL,
    }

    missing = [
        name
        for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )


def create_platform():
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


def get_value(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


def is_rate_limit_error(exc):
    """
    RingCentral may expose HTTP/API errors differently depending
    on SDK version. Detect the known 429/CMN-301 indicators.
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


def get_retry_after(exc):
    """
    Try to retrieve Retry-After from the SDK exception.

    Fall back to 65 seconds, slightly longer than the currently
    observed 60-second RingCentral penalty interval.
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
        "to": [
            {
                "phoneNumber": recipient
            }
        ],
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

    request = builder.request(
        "/restapi/v1.0/account/~/extension/~/fax"
    )

    response = platform.send_request(request)
    data = response.json()

    message_id = get_value(data, "id")
    status = get_value(data, "messageStatus")

    if not message_id:
        raise RuntimeError(
            "RingCentral accepted the request but no message ID "
            "was returned."
        )

    log(f"Fax submitted successfully.")
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
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            log(
                f"Send attempt {attempt}/{MAX_SEND_ATTEMPTS} "
                f"for {recipient}"
            )

            return send_fax(
                sdk,
                platform,
                recipient,
                fax_pdf,
            )

        except Exception as exc:
            if is_rate_limit_error(exc):
                wait_time = get_retry_after(exc)

                log(
                    f"RingCentral rate limit encountered. "
                    f"Waiting {wait_time} seconds before retry."
                )

                time.sleep(wait_time)
                continue

            log(
                f"Fax send attempt {attempt} failed: {exc}"
            )

            if attempt >= MAX_SEND_ATTEMPTS:
                raise

            time.sleep(10)

    raise RuntimeError(
        "Fax failed after maximum retry attempts."
    )


def get_next_pending_job():
    jobs = sorted(
        path
        for path in PENDING_DIR.iterdir()
        if path.is_file()
    )

    if not jobs:
        return None

    return jobs[0]


def claim_job(pending_file):
    """
    Atomically move a pending job into processing so it cannot
    be selected twice.
    """

    processing_file = PROCESSING_DIR / pending_file.name

    pending_file.replace(processing_file)

    return processing_file


def move_completed(processing_file):
    destination = COMPLETED_DIR / processing_file.name
    shutil.move(str(processing_file), str(destination))
    return destination


def move_failed(processing_file):
    destination = FAILED_DIR / processing_file.name
    shutil.move(str(processing_file), str(destination))
    return destination


def process_queue_job(
    processing_file,
    sdk=None,
    platform=None,
    send=False,
):
    log("")
    log("=" * 60)
    log(f"Processing: {processing_file}")
    log("=" * 60)

    raw_data = processing_file.read_bytes()

    result = process_job(
        raw_data,
        send=False,
    )

    fax = result["fax"]
    fax_pdf = result["fax_pdf"]

    if not send:
        log("")
        log("WORKER DRY RUN SUCCESS")
        log(f"Fax:      {fax}")
        log(f"Fax PDF:  {fax_pdf}")
        return result

    message_id = send_with_retry(
        sdk,
        platform,
        fax,
        fax_pdf,
    )

    result["message_id"] = message_id

    return result


def parse_args():
    parser = argparse.ArgumentParser(
        description="Persistent RingCentral fax queue worker."
    )

    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "Actually submit faxes to RingCentral. "
            "Without this option the worker performs dry runs."
        ),
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Process one pending job and exit. "
            "Useful for testing."
        ),
    )

    parser.add_argument(
        "--poll-interval",
        type=int,
        default=2,
        help="Seconds to wait when the queue is empty.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

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

    if args.send:
        try:
            sdk, platform = create_platform()
        except Exception as exc:
            log(
                f"ERROR: Unable to initialize RingCentral: {exc}"
            )
            return 1
    else:
        log(
            "Worker running in DRY-RUN mode. "
            "No faxes will be submitted."
        )

    while True:
        pending_file = get_next_pending_job()

        if pending_file is None:
            if args.once:
                log("No pending jobs.")
                return 0

            time.sleep(args.poll_interval)
            continue

        try:
            processing_file = claim_job(pending_file)
        except FileNotFoundError:
            continue

        try:
            process_queue_job(
                processing_file,
                sdk=sdk,
                platform=platform,
                send=args.send,
            )

            completed_file = move_completed(
                processing_file
            )

            log(
                f"Job completed: {completed_file}"
            )

            if args.send:
                log(
                    f"Pacing next fax for "
                    f"{SEND_INTERVAL} seconds..."
                )

                time.sleep(SEND_INTERVAL)

        except Exception as exc:
            log(
                f"ERROR processing {processing_file}: {exc}"
            )

            try:
                failed_file = move_failed(
                    processing_file
                )

                log(
                    f"Job moved to failed queue: "
                    f"{failed_file}"
                )
            except Exception as move_exc:
                log(
                    f"CRITICAL: Could not move failed job: "
                    f"{move_exc}"
                )

        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
