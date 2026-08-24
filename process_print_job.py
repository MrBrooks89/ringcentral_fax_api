#!/usr/bin/env python3

import re
import sys
import subprocess
from pathlib import Path
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


SPOOL_DIR = Path("/var/spool/ringcentral-fax")
SPOOL_DIR.mkdir(parents=True, exist_ok=True)


def extract(pattern, text):
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def text_to_pdf(text, output_file):
    c = canvas.Canvas(str(output_file), pagesize=letter)

    width, height = letter

    x = 50
    y = height - 50
    line_height = 15

    c.setFont("Courier", 10)

    for line in text.splitlines():
        if y < 50:
            c.showPage()
            c.setFont("Courier", 10)
            y = height - 50

        c.drawString(x, y, line)
        y -= line_height

    c.save()


def main():
    raw_data = sys.stdin.buffer.read()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    raw_file = SPOOL_DIR / f"{timestamp}.raw"
    pdf_file = SPOOL_DIR / f"{timestamp}.pdf"

    raw_file.write_bytes(raw_data)

    text = raw_data.decode("utf-8", errors="ignore")

    fax = extract(r"\{\{fax\s+([^}]+)\}\}", text)
    contact = extract(r"\{\{contact\s+([^}]+)\}\}", text)
    owner = extract(r"\{\{owner\s+([^}]+)\}\}", text)
    winsecid = extract(r"\{\{winsecid\s+([^}]+)\}\}", text)
    billing = extract(r"\{\{billing\s+([^}]+)\}\}", text)

    print(f"Fax:       {fax}")
    print(f"Contact:   {contact}")
    print(f"Owner:     {owner}")
    print(f"WinSecID:  {winsecid}")
    print(f"Billing:   {billing}")

    if not fax:
        print("No fax number found.")
        return 1

    separator = "---DOCUMENT---"

    if separator not in text:
        print(f"Missing document separator: {separator}")
        return 1

    routing_section, document_section = text.split(separator, 1)

    document_section = document_section.strip()

    if not document_section:
        print("Document section is empty.")
        return 1

    print()
    print("Generating PDF from document section...")
    print(f"PDF: {pdf_file}")

    text_to_pdf(document_section, pdf_file)

    print("Submitting generated PDF to RingCentral...")

    subprocess.run(
        [
            "/opt/ringcentral-fax/venv/bin/python",
            "/opt/ringcentral-fax/send_fax.py",
            "--to",
            fax,
            "--file",
            str(pdf_file),
            "--cover",
            "0",
        ],
        check=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
