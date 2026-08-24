# RingCentral Fax API Gateway

A Linux fax gateway that receives print jobs over **LPD (TCP/515)**, processes the incoming document with **CUPS**, converts the document to PDF, and submits the fax through the **RingCentral Fax API** using Python.

## What This Accomplishes

This project provides a bridge between a traditional SAP/Linux print workflow and RingCentral faxing.

```text
┌─────────┐
│   SAP   │
└────┬────┘
     │ Print job
     ▼
┌────────────────────┐
│ Linux Print Server │
└─────────┬──────────┘
          │ LPR / TCP 515
          ▼
┌────────────────────────────────┐
│ Linux RingCentral Fax Gateway  │
│                                │
│ cups-lpd                       │
│      │                         │
│      ▼                         │
│ CUPS fax queue                 │
│      │                         │
│      ▼                         │
│ sapfax backend                 │
│      │                         │
│      ▼                         │
│ process_print_job.py           │
│      ├─ Parse fax metadata     │
│      ├─ Extract document       │
│      └─ Generate PDF           │
│               │                │
│               ▼                │
│          send_fax.py           │
└───────────────┬────────────────┘
                │ HTTPS
                ▼
        ┌─────────────────┐
        │ RingCentral API │
        └────────┬────────┘
                 │
                 ▼
                Fax
```

The upstream Linux print server only needs to know how to send an LPR job to the gateway. RingCentral API authentication, document processing, and fax delivery are handled by the gateway.

## Repository Layout

```text
RingCentral_Fax_API/
├── README.md
├── install.sh
├── requirements.txt
├── process_print_job.py
├── send_fax.py
├── sapfax
├── .env.example
└── test_sap_fax.txt
```

The production installation uses:

```text
/opt/ringcentral-fax/
├── .env
├── process_print_job.py
├── send_fax.py
└── venv/

/usr/lib/cups/backend/sapfax
/var/spool/ringcentral-fax/
```

## Incoming Print Format

The processor expects fax metadata in the incoming print stream. A sanitized example is:

```text
{{rem}}{{fax 555-555-5555}}
{{rem}}{{contact TEST VENDOR}}
{{rem}}{{owner test@example.com}}
{{rem}}{{billing 1234567890}}

---DOCUMENT---

PURCHASE ORDER

PO Number: 1234567890
Vendor: TEST VENDOR

Item                    Qty        Price
------------------------------------------------
Test Product A          10         $5.00
Test Product B           5        $12.50
```

`process_print_job.py` extracts the fax number and document, creates a PDF, and calls `send_fax.py`.

---

# Installation

The supplied `install.sh` automates most of the Linux-side setup on RHEL-compatible systems.

Run it from the repository directory:

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer:

1. Installs CUPS, `cups-lpd`, Python, firewalld utilities, and SELinux management tools.
2. Creates `/opt/ringcentral-fax`.
3. Creates `/var/spool/ringcentral-fax`.
4. Creates the Python virtual environment.
5. Installs `requirements.txt`.
6. Installs `process_print_job.py` and `send_fax.py`.
7. Installs the custom `sapfax` CUPS backend.
8. Configures the spool directory for CUPS/SELinux.
9. Enables CUPS and `cups-lpd.socket`.
10. Creates the CUPS fax queue.
11. Optionally opens TCP/515 in firewalld.

The installer intentionally does **not** create production RingCentral credentials.

## Manual Installation

### 1. Install Packages

```bash
sudo dnf install -y cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils
```

Enable the required services:

```bash
sudo systemctl enable --now cups
sudo systemctl enable --now cups-lpd.socket
```

Verify LPD is listening:

```bash
sudo ss -lntp | grep ':515'
```

Expected:

```text
LISTEN ... *:515
```

### 2. Install the Application

```bash
sudo mkdir -p /opt/ringcentral-fax
sudo cp process_print_job.py send_fax.py /opt/ringcentral-fax/
```

Create the Python environment:

```bash
sudo python3 -m venv /opt/ringcentral-fax/venv
sudo /opt/ringcentral-fax/venv/bin/pip install --upgrade pip
sudo /opt/ringcentral-fax/venv/bin/pip install -r requirements.txt
```

### 3. Configure RingCentral

Copy the example configuration:

```bash
sudo cp .env.example /opt/ringcentral-fax/.env
sudo vi /opt/ringcentral-fax/.env
```

Populate the values required by `send_fax.py`.

Example:

```text
RC_CLIENT_ID=replace_me
RC_CLIENT_SECRET=replace_me
RC_JWT=replace_me
```

Never commit `.env` to Git.

The CUPS backend normally executes as the `lp` account, so it must be able to read the credentials without making them world-readable. One option is:

```bash
sudo chown root:lp /opt/ringcentral-fax/.env
sudo chmod 640 /opt/ringcentral-fax/.env
```

Verify:

```bash
sudo -u lp test -r /opt/ringcentral-fax/.env \
    && echo "ENV readable" \
    || echo "ENV NOT readable"
```

### 4. Create the Fax Spool

```bash
sudo mkdir -p /var/spool/ringcentral-fax
sudo chown lp:lp /var/spool/ringcentral-fax
sudo chmod 750 /var/spool/ringcentral-fax
```

Configure the SELinux file context:

```bash
sudo semanage fcontext -a -t print_spool_t '/var/spool/ringcentral-fax(/.*)?'

sudo restorecon -Rv /var/spool/ringcentral-fax
```

Verify:

```bash
ls -Zd /var/spool/ringcentral-fax
```

The SELinux type should be `print_spool_t`.

### 5. Install the CUPS Backend

```bash
sudo install -o root -g root -m 755 sapfax /usr/lib/cups/backend/sapfax
```

The backend must call the Python virtual environment under `/opt/ringcentral-fax`, not a virtual environment in a user's home directory.

### 6. Create the CUPS Queue

```bash
sudo lpadmin -p sap_rfax -E -v sapfax:/ -m raw
```

Verify:

```bash
lpstat -v sap_rfax
lpstat -p sap_rfax -l
```

Expected:

```text
device for sap_rfax: sapfax:/
printer sap_rfax is idle. enabled ...
```

> **CUPS compatibility note:** raw queues are deprecated in current CUPS releases. This project intentionally uses a raw queue so the application receives the original print stream without printer-driver transformation. Revalidate this design before upgrading to a CUPS version that removes raw queue support.

### 7. Allow LPD Through the Firewall

LPD uses TCP/515.

For initial testing:

```bash
sudo firewall-cmd --add-port=515/tcp
```

For production, restrict TCP/515 to the authorized Linux print server instead of allowing the entire network:

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.0.2.10/32" port port="515" protocol="tcp" accept'

sudo firewall-cmd --reload
```

Replace `192.0.2.10` with the real print-server address.

---

# Testing

Troubleshoot the system from the RingCentral API upward. This isolates each layer instead of debugging the entire chain at once.

## Test 1 — RingCentral API

Use a known PDF:

```bash
sudo /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/send_fax.py --to 555-555-5555 --file /path/to/test_fax.pdf --cover 0 --wait
```

If this succeeds, Python, credentials, Internet/API access, and RingCentral fax submission are working.

## Test 2 — Print Processor

```bash
cat test_sap_fax.txt | sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/process_print_job.py
```

This tests:

```text
SAP-style input
      ↓
metadata parsing
      ↓
PDF generation
      ↓
send_fax.py
      ↓
RingCentral
```

Using `sudo -u lp` is important because it closely matches the account used by the CUPS backend.

## Test 3 — Local CUPS Pipeline

```bash
lp -d sap_rfax test_sap_fax.txt
```

Check the queue:

```bash
lpstat -o sap_rfax
lpstat -p sap_rfax -l
```

Inspect generated files:

```bash
sudo ls -ltr /var/spool/ringcentral-fax
```

A successful job should produce files similar to:

```text
YYYYMMDD-HHMMSS-job.raw
YYYYMMDD-HHMMSS.raw
YYYYMMDD-HHMMSS.pdf
```

## Test 4 — Remote LPD

From the upstream Linux print server, send an LPR job to:

```text
Host:  <FAX_GATEWAY_IP>
Port:  515/tcp
Queue: sap_rfax
```

On the fax gateway:

```bash
sudo tcpdump -ni any 'tcp port 515'
```

This verifies that the upstream print server is reaching the gateway.

---

# Troubleshooting

## Quick Health Check

```bash
echo "=== CUPS ==="
systemctl is-active cups

echo "=== cups-lpd ==="
systemctl is-active cups-lpd.socket

echo "=== TCP/515 ==="
sudo ss -lnt | grep ':515'

echo "=== Queue ==="
lpstat -p sap_rfax -l

echo "=== Pending Jobs ==="
lpstat -o sap_rfax

echo "=== Recent Fax Files ==="
sudo ls -ltr /var/spool/ringcentral-fax | tail

echo "=== SELinux AVCs ==="
sudo ausearch -m AVC -i -ts recent | \
grep -Ei 'cups|sapfax|ringcentral|python'
```

## Nothing Reaches TCP/515

Check:

```bash
sudo ss -lntp | grep ':515'
sudo firewall-cmd --list-all
sudo tcpdump -ni any 'tcp port 515'
```

If `tcpdump` sees no traffic, troubleshoot the upstream print server, routing, network ACLs, or host firewall.

If traffic reaches TCP/515, move up the stack to CUPS.

## LPD Is Not Listening

```bash
sudo systemctl status cups-lpd.socket
sudo systemctl enable --now cups-lpd.socket
sudo ss -lntp | grep ':515'
```

## CUPS Queue Status

```bash
lpstat -v sap_rfax
lpstat -p sap_rfax -l
lpstat -o sap_rfax
```

If a backend failure disabled the queue:

```bash
sudo cancel -a sap_rfax
sudo cupsenable sap_rfax
sudo cupsaccept sap_rfax
```

Then verify:

```bash
lpstat -p sap_rfax -l
```

## CUPS Logs

Watch live:

```bash
sudo journalctl -u cups -f
```

Recent useful messages:

```bash
sudo journalctl -u cups --since "10 minutes ago" --no-pager | grep -Ei 'Job|sapfax|Fax|processor|ERROR|Traceback|Permission|python'
```

Enable temporary debug logging:

```bash
sudo cupsctl --debug-logging
sudo systemctl restart cups
```

Disable it after troubleshooting:

```bash
sudo cupsctl --no-debug-logging
sudo systemctl restart cups
```

## Inspect the Original Print Job

The `*-job.raw` file is the original stream received by the backend.

```bash
sudo ls -ltr /var/spool/ringcentral-fax
sudo less /var/spool/ringcentral-fax/<timestamp>-job.raw
```

For text data:

```bash
sudo cat /var/spool/ringcentral-fax/<timestamp>-job.raw
```

This is the first place to look if SAP changes the print format or fax metadata is no longer parsed correctly.

## PDF Is Not Created

If `*-job.raw` exists but no `.pdf` is created, test the processor directly:

```bash
cat /var/spool/ringcentral-fax/<timestamp>-job.raw | sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/process_print_job.py
```

Look for a Python traceback or parsing error.

## PDF Exists but Fax Is Not Sent

Test `send_fax.py` independently:

```bash
sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/send_fax.py --to 555-555-5555 --file /var/spool/ringcentral-fax/<timestamp>.pdf --cover 0 --wait
```

Check credentials, API errors, DNS, HTTPS connectivity, and RingCentral responses.

## `.env` Permission Errors

Check:

```bash
sudo ls -lZ /opt/ringcentral-fax/.env
sudo -u lp test -r /opt/ringcentral-fax/.env && echo "Readable" || echo "Not readable"
```

Do not use `chmod 777` or otherwise make API credentials world-readable.

## SELinux

Keep SELinux enforcing in production.

Check for denials:

```bash
getenforce
sudo ausearch -m AVC -i -ts recent
```

Verify spool labels:

```bash
ls -Zd /var/spool/ringcentral-fax
```

Restore them if necessary:

```bash
sudo restorecon -Rv /var/spool/ringcentral-fax
```

If SELinux is suspected, use the AVC log to identify the denied operation rather than permanently disabling SELinux.

## `Exec format error`

If CUPS reports:

```text
execv failed: Exec format error
```

verify the backend:

```bash
head -1 /usr/lib/cups/backend/sapfax
file /usr/lib/cups/backend/sapfax
ls -l /usr/lib/cups/backend/sapfax
```

The script needs a valid shebang, such as:

```bash
#!/bin/bash
```

and must be executable:

```bash
sudo chmod 755 /usr/lib/cups/backend/sapfax
```

## Python `Permission denied`

Make sure the backend calls:

```text
/opt/ringcentral-fax/venv/bin/python
```

Do not use a Python virtual environment under a user's home directory.

Test:

```bash
sudo -u lp /opt/ringcentral-fax/venv/bin/python --version
```

---

# Troubleshooting Flow

```text
Fax not received
      │
      ▼
Did TCP/515 reach gateway?
      │
 ┌────┴────┐
 NO       YES
 │         │
 ▼         ▼
Network   Did CUPS receive job?
          │
     ┌────┴────┐
     NO       YES
     │         │
     ▼         ▼
 cups-lpd     Was *-job.raw created?
 / queue       │
          ┌────┴────┐
          NO       YES
          │         │
          ▼         ▼
       backend     Was PDF created?
       / SELinux    │
               ┌────┴────┐
               NO       YES
               │         │
               ▼         ▼
             parser    Does send_fax.py
             / PDF     work manually?
                         │
                    ┌────┴────┐
                    NO       YES
                    │         │
                    ▼         ▼
                 API/.env   Inspect job/API
```

---

# Security

For production:

- Restrict TCP/515 to authorized print servers.
- Keep SELinux enforcing.
- Protect RingCentral credentials.
- Do not commit `.env`.
- Use a dedicated RingCentral service account where possible.
- Treat captured print streams and generated PDFs as sensitive data.
- Establish retention/cleanup for `/var/spool/ringcentral-fax`.
- Monitor disabled CUPS queues and failed API submissions.
- Rotate API credentials according to organizational policy.

---

# `.gitignore`

Recommended entries:

```gitignore
.env
venv/
.venv/
__pycache__/
*.py[cod]
*.log
*.raw
*.pdf
```

Do not commit production fax documents, raw spool captures, JWTs, client secrets, or other credentials.
