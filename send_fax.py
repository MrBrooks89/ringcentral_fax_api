from dotenv import load_dotenv

load_dotenv()

import argparse
import os
import sys
import time
from pathlib import Path

from ringcentral import SDK



CLIENT_ID = os.getenv("RC_CLIENT_ID")
CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET")
JWT_TOKEN = os.getenv("RC_JWT_TOKEN")
SERVER_URL = os.getenv("RC_SERVER")



required_env = {
    "RC_CLIENT_ID": CLIENT_ID,
    "RC_CLIENT_SECRET": CLIENT_SECRET,
    "RC_JWT_TOKEN": JWT_TOKEN,
    "RC_SERVER": SERVER_URL,
}

missing = [name for name, value in required_env.items() if not value]

if missing:
    print(f"Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)


parser = argparse.ArgumentParser(
    description="Send a fax using the RingCentral Fax API."
)

parser.add_argument(
    "--to",
    required=True,
    help="Destination fax number. Example: +19035551212",
)

parser.add_argument(
    "--file",
    required=True,
    help="Document to fax. Example: purchase_order.pdf",
)

parser.add_argument(
    "--resolution",
    choices=["High", "Low"],
    default="High",
    help="Fax resolution. Default: High",
)

parser.add_argument(
    "--cover-text",
    help="Optional text to include on the fax cover page.",
)

parser.add_argument(
    "--wait",
    action="store_true",
    help="Wait and monitor the fax until it leaves the Queued state.",
)

parser.add_argument(
    "--cover",
    type=int,
    choices=range(0, 14),
    default=0,
    help="RingCentral fax cover page template (0-13). 0 = no cover page.",
)

args = parser.parse_args()



fax_file = Path(args.file)

if not fax_file.exists():
    print(f"File does not exist: {fax_file}")
    sys.exit(1)

if not fax_file.is_file():
    print(f"Not a file: {fax_file}")
    sys.exit(1)

if fax_file.stat().st_size == 0:
    print(f"File is empty: {fax_file}")
    sys.exit(1)


# RingCentral currently limits all attachments in a fax request to 50 MB combined.
MAX_FILE_SIZE = 50 * 1024 * 1024

if fax_file.stat().st_size > MAX_FILE_SIZE:
    print("File exceeds RingCentral's 50 MB fax attachment limit.")
    sys.exit(1)


rcsdk = SDK(
    CLIENT_ID,
    CLIENT_SECRET,
    SERVER_URL,
)

platform = rcsdk.platform()

try:
    platform.login(jwt=JWT_TOKEN)
except Exception as e:
    print(f"Unable to authenticate to RingCentral: {e}")
    sys.exit(1)


def get_value(obj, name, default=None):
    """
    RingCentral SDK responses can behave either like objects
    or dictionaries depending on SDK/version.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


def check_fax_status(message_id, poll_interval=10):
    """
    Poll the RingCentral message store until the fax is no
    longer queued.
    """

    endpoint = (
        f"/restapi/v1.0/account/~/extension/~/message-store/{message_id}"
    )

    while True:
        try:
            resp = platform.get(endpoint)
            data = resp.json()

            status = get_value(data, "messageStatus")
            fax_resolution = get_value(data, "faxResolution")
            fax_page_count = get_value(data, "faxPageCount")
            creation_time = get_value(data, "creationTime")

            print(f"Message ID:   {message_id}")
            print(f"Status:       {status}")

            if creation_time:
                print(f"Created:      {creation_time}")

            if fax_resolution:
                print(f"Resolution:   {fax_resolution}")

            if fax_page_count is not None:
                print(f"Pages:        {fax_page_count}")

            print()

            if status != "Queued":
                return data

            time.sleep(poll_interval)

        except Exception as e:
            print(f"Unable to retrieve fax status: {e}")
            return None


def send_fax(
    recipient,
    filename,
    resolution="High",
    cover_index=0,
    cover_text=None,
):
    """
    Send a document using RingCentral's Fax API.
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

    if cover_text:
        body["coverPageText"] = cover_text

    try:
        builder = rcsdk.create_multipart_builder()

        builder.set_body(body)


        with open(filename, "rb") as file_handle:
            content = file_handle.read()

        attachment = (
            Path(filename).name,
            content,
        )

        builder.add(attachment)

        request = builder.request(
            "/restapi/v1.0/account/~/extension/~/fax"
        )

        response = platform.send_request(request)
        data = response.json()

        message_id = get_value(data, "id")
        message_status = get_value(data, "messageStatus")

        print("Fax submitted successfully.")
        print(f"To:           {recipient}")
        print(f"File:         {filename}")
        print(f"Message ID:   {message_id}")

        if message_status:
            print(f"Status:       {message_status}")

        return message_id

    except Exception as e:
        print(f"Unable to send fax: {e}")
        return None


message_id = send_fax(
    recipient=args.to,
    filename=fax_file,
    resolution=args.resolution,
    cover_index=args.cover,
    cover_text=args.cover_text,
)

if not message_id:
    sys.exit(1)

if args.wait:
    print("\nMonitoring fax status...\n")
    check_fax_status(message_id)