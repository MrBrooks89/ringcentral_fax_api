#!/usr/bin/env python3

import re
import sys
import subprocess
from pathlib import Path
from datetime import datetime

from pypdf import PdfReader, PdfWriter


SPOOL_DIR = Path("/var/spool/ringcentral-fax")
GPDL = Path("/opt/ringcentral-fax/bin/gpdl")

SPOOL_DIR.mkdir(parents=True, exist_ok=True)


def extract(pattern, text):
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None


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

    if len(reader.pages) < 2:
        raise RuntimeError(
            f"Expected routing page + document, but PDF only has "
            f"{len(reader.pages)} page(s)"
        )

    first_page_text = reader.pages[0].extract_text() or ""

    # Don't blindly delete page 1.
    # Verify that it looks like a RightFax routing page first.
    routing_markers = [
        "{{fax",
        "{{contact",
        "{{billing",
        "{{winsecid",
    ]

    marker_count = sum(
        marker.lower() in first_page_text.lower()
        for marker in routing_markers
    )

    if marker_count < 2:
        raise RuntimeError(
            "First PDF page does not look like a RightFax routing page. "
            "Refusing to remove it."
        )

    writer = PdfWriter()

    # Skip routing page.
    for page in reader.pages[1:]:
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)


def main():
    raw_data = sys.stdin.buffer.read()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    raw_file = SPOOL_DIR / f"{timestamp}.pcl"
    full_pdf = SPOOL_DIR / f"{timestamp}-full.pdf"
    fax_pdf = SPOOL_DIR / f"{timestamp}.pdf"

    # Preserve exactly what SAP sent.
    raw_file.write_bytes(raw_data)

    # PCL contains ASCII routing commands even though the entire
    # stream is binary. latin-1 preserves every byte 1:1.
    text = raw_data.decode("latin-1", errors="ignore")

    fax = extract(
        r"\{\{rem\}\}\{\{fax\s+([^}]*)\}\}",
        text,
    )
    contact = extract(
        r"\{\{rem\}\}\{\{contact\s+([^}]*)\}\}",
        text,
    )
    owner = extract(
        r"\{\{rem\}\}\{\{owner\s*([^}]*)\}\}",
        text,
    )
    winsecid = extract(
        r"\{\{rem\}\}\{\{winsecid\s+([^}]*)\}\}",
        text,
    )
    billing = extract(
        r"\{\{rem\}\}\{\{billing\s+([^}]*)\}\}",
        text,
    )

    print(f"Fax:       {fax}")
    print(f"Contact:   {contact}")
    print(f"Owner:     {owner}")
    print(f"WinSecID:  {winsecid}")
    print(f"Billing:   {billing}")
    print(f"Raw PCL:   {raw_file}")

    if not fax:
        print("ERROR: No fax number found in PCL job.")
        return 1

    print("Converting PCL to PDF...")

    try:
        convert_pcl_to_pdf(raw_file, full_pdf)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: GhostPDL conversion failed: {exc}")
        return 1

    print(f"Rendered PDF: {full_pdf}")

    print("Removing RightFax routing page...")

    try:
        remove_routing_page(full_pdf, fax_pdf)
    except Exception as exc:
        print(f"ERROR: Could not safely remove routing page: {exc}")
        return 1

    print(f"Fax PDF: {fax_pdf}")

    print("Submitting PDF to RingCentral...")

    try:
        subprocess.run(
            [
                "/opt/ringcentral-fax/venv/bin/python",
                "/opt/ringcentral-fax/send_fax.py",
                "--to",
                fax,
                "--file",
                str(fax_pdf),
                "--cover",
                "0",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: RingCentral send failed: {exc}")
        return 1

    print("Fax submitted successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
