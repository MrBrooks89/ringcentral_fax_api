#!/usr/bin/env python3

import argparse
import re
import sys
import subprocess
from pathlib import Path
from datetime import datetime

from pypdf import PdfReader, PdfWriter


SPOOL_DIR = Path("/var/spool/ringcentral-fax")
GPDL = Path("/opt/ringcentral-fax/bin/gpdl")
PYTHON = Path("/opt/ringcentral-fax/venv/bin/python")
SEND_FAX = Path("/opt/ringcentral-fax/send_fax.py")

SPOOL_DIR.mkdir(parents=True, exist_ok=True)


def extract(pattern, text):
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_metadata(raw_data):
    """
    Extract the legacy RightFax routing metadata embedded as
    printable ASCII inside the binary SAP PJL/PCL stream.
    """

    text = raw_data.decode("latin-1", errors="ignore")

    return {
        "fax": extract(
            r"\{\{rem\}\}\{\{fax\s+([^}]*)\}\}",
            text,
        ),
        "contact": extract(
            r"\{\{rem\}\}\{\{contact\s+([^}]*)\}\}",
            text,
        ),
        "owner": extract(
            r"\{\{rem\}\}\{\{owner\s*([^}]*)\}\}",
            text,
        ),
        "winsecid": extract(
            r"\{\{rem\}\}\{\{winsecid\s+([^}]*)\}\}",
            text,
        ),
        "billing": extract(
            r"\{\{rem\}\}\{\{billing\s+([^}]*)\}\}",
            text,
        ),
        "notifyhost": extract(
            r"\{\{rem\}\}\{\{notifyhost\s+([^}]*)\}\}",
            text,
        ),
    }


def convert_pcl_to_pdf(raw_file, pdf_file):
    subprocess.run(
        [
            str(GPDL),
            "-dNOPAUSE",
            "-dBATCH",
            "-sDEVICE=pdfwrite",
            f"-sOutputFile={pdf_file}",
            str(raw_file),
        ],
        check=True,
    )


def remove_routing_page(input_pdf, output_pdf):
    reader = PdfReader(str(input_pdf))

    # Expected SAP/RightFax layout:
    #
    #   Page 1 = RightFax routing page
    #   Page 2+ = actual fax document
    #
    if len(reader.pages) < 2:
        raise RuntimeError(
            f"Expected routing page + document, but PDF only has "
            f"{len(reader.pages)} page(s)"
        )

    writer = PdfWriter()

    # Keep all pages except the legacy RightFax routing page.
    for page in reader.pages[1:]:
        writer.add_page(page)

    with open(output_pdf, "wb") as file_handle:
        writer.write(file_handle)


def send_via_existing_script(fax, fax_pdf):
    """
    Preserve the current working RingCentral send method.

    This will be replaced by the persistent worker later so that
    RingCentral authentication can be reused across multiple jobs.
    """

    subprocess.run(
        [
            str(PYTHON),
            str(SEND_FAX),
            "--to",
            fax,
            "--file",
            str(fax_pdf),
            "--cover",
            "0",
        ],
        check=True,
    )


def process_job(raw_data, send=True):
    """
    Process one complete SAP PJL/PCL fax job.

    Returns a dictionary containing the extracted metadata and the
    generated file paths. This function can later be imported directly
    by fax_worker.py.
    """

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    raw_file = SPOOL_DIR / f"{timestamp}.pcl"
    full_pdf = SPOOL_DIR / f"{timestamp}-full.pdf"
    fax_pdf = SPOOL_DIR / f"{timestamp}.pdf"

    # Preserve exactly what SAP sent.
    raw_file.write_bytes(raw_data)

    metadata = extract_metadata(raw_data)

    fax = metadata["fax"]
    contact = metadata["contact"]
    owner = metadata["owner"]
    winsecid = metadata["winsecid"]
    billing = metadata["billing"]
    notifyhost = metadata["notifyhost"]

    print(f"Fax:        {fax}")
    print(f"Contact:    {contact}")
    print(f"Owner:      {owner}")
    print(f"WinSecID:   {winsecid}")
    print(f"Billing:    {billing}")
    print(f"NotifyHost: {notifyhost}")
    print(f"Raw PCL:    {raw_file}")

    #
    # Fax number is mandatory.
    #
    if not fax:
        raise RuntimeError("No fax number found in PCL job.")

    #
    # Require additional RightFax metadata before assuming that
    # page 1 is safe to remove.
    #
    if not any([contact, billing, winsecid]):
        raise RuntimeError(
            "Expected RightFax routing metadata was not found. "
            "Refusing to remove page 1."
        )

    print()
    print("RightFax routing metadata detected.")
    print("Converting PCL to PDF...")

    convert_pcl_to_pdf(raw_file, full_pdf)

    print(f"Rendered PDF: {full_pdf}")
    print("Removing RightFax routing page...")

    remove_routing_page(full_pdf, fax_pdf)

    print(f"Fax PDF: {fax_pdf}")

    result = {
        **metadata,
        "raw_file": raw_file,
        "full_pdf": full_pdf,
        "fax_pdf": fax_pdf,
    }

    if send:
        print("Submitting PDF to RingCentral...")

        send_via_existing_script(fax, fax_pdf)

        print("Fax submitted successfully.")
    else:
        print()
        print("DRY RUN SUCCESS")
        print(f"Destination fax: {fax}")
        print(f"Final fax PDF:   {fax_pdf}")
        print("RingCentral submission is disabled for this run.")

    return result


def parse_args():
    parser = argparse.ArgumentParser(
        description="Process an SAP PJL/PCL fax print job."
    )

    parser.add_argument(
        "--file",
        type=Path,
        help=(
            "Read the SAP print job from a file instead of stdin. "
            "If omitted, the job is read from stdin."
        ),
    )

    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Process the job but do not submit the fax to RingCentral.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.file:
        if not args.file.exists():
            print(f"ERROR: Input file does not exist: {args.file}")
            return 1

        if not args.file.is_file():
            print(f"ERROR: Input path is not a file: {args.file}")
            return 1

        raw_data = args.file.read_bytes()
    else:
        # Backward-compatible CUPS/sapfax behavior.
        raw_data = sys.stdin.buffer.read()

    if not raw_data:
        print("ERROR: Input print job is empty.")
        return 1

    try:
        process_job(
            raw_data,
            send=not args.no_send,
        )
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: External command failed: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: Fax processing failed: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
