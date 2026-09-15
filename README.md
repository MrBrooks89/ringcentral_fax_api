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
│      ├─ Save incoming PCL      │
│      ├─ GhostPDL → PDF         │
│      └─ Remove routing page    │
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
├── bin/
│   └── gpdl
├── process_print_job.py
├── send_fax.py
└── venv/

/usr/lib/cups/backend/sapfax
/var/spool/ringcentral-fax/
```

## Incoming Print Format

SAP sends the fax job as a **raw PJL/PCL print stream**, not as a PDF or plain-text document. CUPS intentionally preserves this stream without applying a printer-driver conversion.

The PCL data contains legacy RightFax routing commands as printable ASCII metadata. A sanitized example is:

```text
{{rem}}{{fax 555-555-5555}}
{{rem}}{{contact TEST VENDOR}}
{{rem}}{{owner test@example.com}}
{{rem}}{{winsecid 00000000}}
{{rem}}{{billing 1234567890}}
{{rem}}{{notifyhost success.inc failure.inc sendmail}}
```

The remainder of the stream contains the PCL representation of the purchase order. In the tested SAP output, the RightFax routing information renders as the first page and the purchase order begins on the following page.

`process_print_job.py` reads the original bytes from standard input, extracts the fax metadata, saves the incoming stream as `.pcl`, and uses **GhostPDL (`gpdl`)** to render the PCL to PDF. It then removes the legacy RightFax routing page and passes the cleaned purchase-order PDF to `send_fax.py`.

The `.raw` extension used by CUPS does not indicate the document format. It only means that CUPS preserved the original print data. The underlying SAP job is PJL/PCL.


## RightFax Metadata Mapping and Notification Differences

The incoming PCL stream contains legacy RightFax metadata. The currently observed fields are:

```text
fax         Required destination fax number.
contact     Retained for logging and job correlation.
owner       Legacy RightFax metadata; may be blank.
winsecid    Legacy RightFax/Windows identifier.
billing     Useful for PO/job correlation.
notifyhost  Legacy RightFax notification directive.
```

Only the fax number is required to submit the document to RingCentral. The other values can be retained for logging, troubleshooting, correlation, or future workflow logic.

RightFax per-job notification behavior is not currently reproduced exactly by RingCentral. RingCentral notification recipients configured on the SAP Fax account are static, so configured recipients may receive notifications for all faxes sent by that account rather than only the user associated with one specific fax job.

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

> **Current installer note:** the README reflects the newer SAP PJL/PCL processing path. If `install.sh` has not yet been updated to install GhostPDL/`gpdl`, `pypdf`, and the revised processor permissions, perform those steps manually after running the installer.

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
sudo chown root:lp /opt/ringcentral-fax/process_print_job.py /opt/ringcentral-fax/send_fax.py
sudo chmod 750 /opt/ringcentral-fax/process_print_job.py /opt/ringcentral-fax/send_fax.py
```

Avoid `chmod 777` on application scripts. For administrative edits, use `sudoedit` or an editor through `sudo` instead of making the files world-writable.

Create the Python environment:

```bash
sudo python3 -m venv /opt/ringcentral-fax/venv
sudo /opt/ringcentral-fax/venv/bin/pip install --upgrade pip
sudo /opt/ringcentral-fax/venv/bin/pip install -r requirements.txt
```

### 2a. Install GhostPDL

SAP sends PJL/PCL, so the gateway requires GhostPDL with PCL support. On the tested RHEL 9 system, `gpdl` was built from the official GhostPDL 10.08.0 source because a suitable `gpcl6`/GhostPCL package was not available from the configured RHEL repositories.

The tested build procedure was:

```bash
cd /tmp
wget https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10080/ghostpdl-10.08.0.tar.gz
tar -xzf ghostpdl-10.08.0.tar.gz
cd ghostpdl-10.08.0
./configure
make -j"$(nproc)"
```

After the build completes, install the resulting `gpdl` executable into the application tree:

```bash
sudo install -d -o root -g root -m 755 /opt/ringcentral-fax/bin
sudo install -o root -g root -m 755 /tmp/ghostpdl-10.08.0/bin/gpdl /opt/ringcentral-fax/bin/gpdl
```

The processor is configured to use:

```text
/opt/ringcentral-fax/bin/gpdl
```

Verify that the CUPS `lp` account can execute it:

```bash
sudo -u lp /opt/ringcentral-fax/bin/gpdl --version
```

Also verify its shared-library dependencies:

```bash
ldd /opt/ringcentral-fax/bin/gpdl | grep 'not found'
```

If `ldd` prints nothing, no linked libraries are missing. Finally, run a known captured SAP PCL file through the installed binary before putting the gateway into service.

Do not rely on a binary left under `/tmp`; the working `gpdl` binary must be copied into a persistent production path.

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

> **CUPS compatibility note:** this gateway currently uses both a CUPS raw queue and a traditional custom backend so the original SAP PJL/PCL stream reaches the application unchanged. These are legacy CUPS mechanisms and may not remain available in future major CUPS releases. Validate the complete fax path before allowing major CUPS upgrades.

### Long-Term CUPS Migration Path

CUPS is currently used mainly as an LPD receiver, spooler, queue manager, and launcher for the `sapfax` backend. If a future CUPS release removes raw queues or traditional backends, the preferred migration path is to replace the CUPS/LPD layer with a dedicated LPD receiver.

The long-term flow would be:

```text
SAP → LPD/TCP 515 → dedicated LPD receiver → process_print_job.py
    → GhostPDL → cleaned PDF → RingCentral
```

This preserves the existing SAP output format and allows the PCL parsing, GhostPDL conversion, PDF cleanup, and RingCentral API portions of the project to remain largely unchanged.

### CUPS Version Hold

The gateway has been validated with:

- RHEL 9
- CUPS 2.3.3op2-39.el9_8
- `cups-lpd`
- Raw CUPS queue
- Custom `sapfax` backend

Because the gateway depends on functionality deprecated by newer CUPS
architectures, CUPS packages can be excluded from normal DNF updates:

Add the following under the existing `[main]` section in `/etc/dnf/dnf.conf`:

```ini
# CUPS held at known-working version for SAP -> RingCentral fax gateway.
# Validate the complete fax pipeline before allowing CUPS upgrades.
excludepkgs=cups*
```

Record the current known-good package versions:

```bash
rpm -qa | grep '^cups' | sort | \
  sudo tee /opt/ringcentral-fax/cups-known-good-versions.txt
```

Before allowing CUPS upgrades, validate the complete path:

```text
SAP → LPD/515 → CUPS → sapfax → PCL → GhostPDL → PDF → RingCentral
```

### Queue Naming

This README uses `sap_rfax` as the production queue name. Development and testing systems may use a name such as `sap_rfax_test`; substitute the actual queue name in `lpstat`, `cupsenable`, `cupsaccept`, `cancel`, and other CUPS commands.

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

Troubleshoot from the RingCentral API upward so each layer can be verified independently. During PCL conversion testing, it is safest to temporarily disable/comment the `send_fax.py` subprocess call so a captured production PO cannot accidentally be transmitted.

## Test 1 — RingCentral API

Use a known PDF:

```bash
sudo -u lp /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/send_fax.py \
  --to 555-555-5555 \
  --file /path/to/test_fax.pdf \
  --cover 0 --wait
```

If this succeeds, Python, credentials, network/API access, and RingCentral fax submission are working.

## Test 2 — Captured SAP PCL

Use an actual captured SAP print stream. If the test file is under `/tmp`, make sure `lp` can read it:

```bash
sudo setfacl -m u:lp:r /tmp/sap-po.pcl
sudo -u lp head -c 20 /tmp/sap-po.pcl | xxd
```

Run the processor as the same account used by the CUPS backend:

```bash
sudo -u lp sh -c \
  '/opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/process_print_job.py < /tmp/sap-po.pcl'
```

A successful dry run should produce files similar to:

```text
YYYYMMDD-HHMMSS-ffffff.pcl
YYYYMMDD-HHMMSS-ffffff-full.pdf
YYYYMMDD-HHMMSS-ffffff.pdf
```

The `-full.pdf` file is the GhostPDL rendering and may contain the legacy RightFax routing page. The final `.pdf` should contain only the fax document that will be submitted to RingCentral.

## Test 3 — End-to-End SAP/CUPS Pipeline

With the RingCentral send step still disabled for a dry run, have SAP resend a real fax/PO job to the CUPS queue. Then check:

```bash
lpstat -p sap_rfax -l
lpstat -o sap_rfax
sudo ls -ltr /var/spool/ringcentral-fax
```

Watch CUPS while the job arrives:

```bash
sudo journalctl -u cups -f
```

This validates the complete local path:

```text
SAP → LPD/TCP 515 → cups-lpd → CUPS raw queue → sapfax
    → process_print_job.py → PCL metadata extraction → GhostPDL
    → routing-page removal → final PDF
```

Once this succeeds, re-enable the `send_fax.py` subprocess and perform a controlled live fax test.

## Test 4 — Verify Remote LPD Traffic

SAP/upstream printing should target:

```text
Host:  <FAX_GATEWAY_IP>
Port:  515/tcp
Queue: sap_rfax
```

On the fax gateway:

```bash
sudo tcpdump -nni any 'tcp port 515'
```

This verifies that the source system is reaching the gateway.

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

The backend may initially receive or save the CUPS job with a `.raw` name. This is the original byte stream from SAP; the tested SAP data is PJL/PCL, not plain text or PDF.

```bash
sudo ls -ltr /var/spool/ringcentral-fax
sudo file /var/spool/ringcentral-fax/<timestamp>-job.raw
sudo xxd -l 512 /var/spool/ringcentral-fax/<timestamp>-job.raw
```

A PJL/PCL job normally begins with a PJL universal-exit sequence and includes a command similar to `@PJL ENTER LANGUAGE=PCL`. The embedded `{{rem}}{{fax ...}}` metadata can still be visible as ASCII inside the binary PCL stream.

This is the first place to look if SAP changes the print format or fax metadata is no longer parsed correctly.

## PDF Is Not Created

If the incoming job exists but no `.pdf` is created, verify GhostPDL first:

```bash
sudo -u lp /opt/ringcentral-fax/bin/gpdl --version
```

Then test the processor directly. Because shell redirection happens before `sudo` unless it is placed inside the shell run as `lp`, use:

```bash
sudo -u lp sh -c \
  '/opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/process_print_job.py < /path/to/captured-job.pcl'
```

Look for metadata parsing errors, GhostPDL conversion failures, `pypdf` errors, permission problems, or SELinux denials.

## PDF Exists but Fax Is Not Sent

Test `send_fax.py` independently:

```bash
sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/send_fax.py --to 555-555-5555 --file /var/spool/ringcentral-fax/<timestamp>.pdf --cover 0 --wait
```

Check credentials, API errors, DNS, HTTPS connectivity, and RingCentral responses.

## GhostPDL / PCL Conversion Errors

Confirm the captured job is PCL/PJL:

```bash
file /path/to/captured-job
xxd -l 512 /path/to/captured-job
```

A tested SAP job contained PJL followed by `@PJL ENTER LANGUAGE=PCL`. Confirm `gpdl` works as the CUPS account:

```bash
sudo -u lp /opt/ringcentral-fax/bin/gpdl --version
```

If it works interactively but fails from CUPS, check filesystem permissions and SELinux AVCs rather than disabling SELinux:

```bash
namei -l /opt/ringcentral-fax/bin/gpdl
sudo ausearch -m AVC,USER_AVC -ts recent
```

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
 cups-lpd     Was PCL job captured?
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

# Spool Retention and Cleanup

Each processed fax can leave multiple files in `/var/spool/ringcentral-fax`, including the captured PCL, the full GhostPDL-rendered PDF, and the final cleaned PDF. These files may contain purchase-order data and should not be retained indefinitely.

Choose a retention period that matches operational and compliance requirements. A simple example that removes processor artifacts older than 14 days is:

```bash
sudo find /var/spool/ringcentral-fax -type f -mtime +14 -delete
```

For production, run cleanup from a controlled `systemd` timer or equivalent scheduled job, and test the retention rule before enabling automatic deletion. Do not delete active jobs that are still being processed or investigated.

---

# Security

For production:

- Restrict TCP/515 to authorized print servers.
- Keep SELinux enforcing.
- Protect RingCentral credentials.
- Keep application scripts owned by an administrative account and not world-writable (`root:lp`, mode `750` is suitable for the tested layout).
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
