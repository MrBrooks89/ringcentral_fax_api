# RingCentral Fax API Gateway

A Linux fax gateway that receives SAP print jobs over **LPD (TCP/515)**, preserves the original PJL/PCL stream with **CUPS**, converts the document to PDF with **GhostPDL**, and submits faxes through the **RingCentral Fax API** using a persistent Python worker.

The gateway is designed to replace a legacy RightFax workflow without requiring SAP to change its existing print output.

---

# Architecture

The production design separates **job ingestion** from **fax processing**.

```text
┌─────────┐
│   SAP   │
└────┬────┘
     │ LPR / TCP 515
     ▼
┌─────────────────────────────┐
│ Linux RingCentral Gateway   │
│                             │
│ cups-lpd                    │
│     │                       │
│     ▼                       │
│ CUPS raw queue              │
│     │                       │
│     ▼                       │
│ sapfax backend              │
│     │                       │
│     ├─ Save original job    │
│     │  into pending/        │
│     │                       │
│     └─ Return success       │
│        immediately to CUPS  │
└──────────────┬──────────────┘
               │
               ▼
     /var/spool/ringcentral-fax/
               │
               ▼
          pending/
               │
               ▼
┌─────────────────────────────┐
│ ringcentral-fax-worker      │
│                             │
│ Authenticate once           │
│ Reuse RingCentral session   │
│ Process jobs sequentially   │
│ Pace outbound submissions   │
│ Retry transient failures    │
│                             │
│ process_print_job.py        │
│   ├─ Parse RightFax metadata│
│   ├─ Save PCL               │
│   ├─ GhostPDL → PDF         │
│   └─ Remove routing page    │
└──────────────┬──────────────┘
               │ HTTPS
               ▼
        ┌───────────────┐
        │ RingCentral   │
        │ Fax API       │
        └───────┬───────┘
                │
                ▼
               Fax
```

This design prevents a temporary RingCentral/API problem from disabling the CUPS printer. CUPS only needs to safely persist the incoming SAP print job. The long-running worker handles document conversion and RingCentral submission independently.

---

# Why a Persistent Worker Is Used

The original implementation launched `send_fax.py` as a new process for every print job. Each process authenticated separately with RingCentral, which caused bulk SAP fax batches to hit the RingCentral **Auth** throttling limit.

Observed application limits during testing:

```text
Auth:   5 requests / 60 seconds
Heavy: 10 requests / 60 seconds
```

A bulk test consistently processed five jobs and then received:

```text
POST /restapi/oauth/token
HTTP 429
CMN-301
Request rate exceeded
```

The persistent worker fixes this by authenticating once when the service starts and reusing the same RingCentral SDK/platform session for multiple fax submissions.

A six-fax test produced:

```text
1 x POST /restapi/oauth/token  -> 200
6 x POST /restapi/.../fax     -> 200
```

The worker also spaces fax submissions by approximately seven seconds so normal batch processing remains below the observed Heavy request limit.

---

# Repository Layout

```text
RingCentral_Fax_API/
├── README.md
├── install.sh
├── requirements.txt
├── process_print_job.py
├── send_fax.py
├── fax_worker.py
├── ringcentral-fax-worker.service
├── sapfax
├── .env.example
└── test_sap_fax.txt
```

Production layout:

```text
/opt/ringcentral-fax/
├── .env
├── bin/
│   └── gpdl
├── process_print_job.py
├── send_fax.py
├── fax_worker.py
└── venv/

/usr/lib/cups/backend/sapfax
/etc/systemd/system/ringcentral-fax-worker.service
/etc/tmpfiles.d/ringcentral-fax.conf

/var/spool/ringcentral-fax/
├── pending/
├── processing/
├── completed/
├── failed/
└── artifacts/
```

---

# Incoming SAP Print Format

SAP sends the fax job as a **raw PJL/PCL print stream**, not as a PDF or normal text document.

CUPS uses a raw queue so the original byte stream reaches the gateway without printer-driver conversion.

The PCL stream contains legacy RightFax routing metadata as printable ASCII. A sanitized example:

```text
{{rem}}{{fax 555-555-5555}}
{{rem}}{{contact TEST VENDOR}}
{{rem}}{{owner test@example.com}}
{{rem}}{{winsecid 00000000}}
{{rem}}{{billing 1234567890}}
{{rem}}{{notifyhost success.inc failure.inc sendmail}}
```

Observed fields:

```text
fax         Required destination fax number.
contact     Vendor/contact information.
owner       Legacy RightFax metadata; may be blank.
winsecid    Legacy RightFax/Windows identifier.
billing     PO/job correlation value.
notifyhost  Legacy RightFax notification directive.
```

The `.raw` extension used by CUPS does **not** describe the true document format. It means CUPS preserved the incoming stream. The tested SAP jobs contain PJL followed by PCL.

A typical captured job contains:

```text
@PJL ENTER LANGUAGE=PCL
```

---

# Document Processing

`process_print_job.py` performs the document-processing portion of the workflow.

It:

1. Reads the original SAP PJL/PCL bytes.
2. Extracts the embedded RightFax metadata.
3. Saves the original print stream as `.pcl`.
4. Uses GhostPDL `gpdl` to render the PCL to PDF.
5. Removes the legacy RightFax routing page.
6. Returns the cleaned fax PDF and metadata to the worker.

The tested SAP output renders as:

```text
Page 1   RightFax routing metadata
Page 2+  Purchase order / actual fax document
```

The processor refuses to blindly remove page 1 unless expected RightFax metadata is present.

The processor supports both stdin and file-based operation.

Dry-run from a captured file:

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/process_print_job.py \
  --file /path/to/job.raw \
  --no-send
```

Backward-compatible stdin testing:

```bash
sudo -u lp sh -c \
'/opt/ringcentral-fax/venv/bin/python \
 /opt/ringcentral-fax/process_print_job.py \
 --no-send \
 < /path/to/job.raw'
```

`send_fax.py` remains useful as a standalone API diagnostic tool, but the production worker submits faxes directly through its persistent RingCentral session.

---

# Queue and Worker Behavior

The CUPS `sapfax` backend does **not** perform PCL conversion or call RingCentral.

Its job is intentionally small:

```text
Receive complete CUPS job
        ↓
Write temporary file in pending/
        ↓
Atomically rename to *.raw
        ↓
Return exit 0 to CUPS
```

The temporary-file/rename pattern prevents the worker from seeing a partially written SAP job.

The worker then handles:

```text
pending/
   ↓
processing/
   ↓
PCL conversion
   ↓
PDF cleanup
   ↓
RingCentral submission
   ↓
completed/
```

If processing fails:

```text
processing/
   ↓
failed/
```

A RingCentral failure therefore does not disable the CUPS queue or prevent later SAP print jobs from being accepted.

---

# Installation

The gateway has been tested on RHEL 9.

## 1. Install Required Packages

```bash
sudo dnf install -y \
  cups \
  cups-lpd \
  python3 \
  python3-pip \
  firewalld \
  policycoreutils-python-utils
```

Enable CUPS and LPD:

```bash
sudo systemctl enable --now cups
sudo systemctl enable --now cups-lpd.socket
```

Verify TCP/515:

```bash
sudo ss -lntp | grep ':515'
```

Expected:

```text
LISTEN ... *:515
```

---

## 2. Install the Application

```bash
sudo mkdir -p /opt/ringcentral-fax
sudo mkdir -p /opt/ringcentral-fax/bin
```

Copy the application:

```bash
sudo cp process_print_job.py send_fax.py fax_worker.py \
  /opt/ringcentral-fax/
```

Recommended ownership and permissions:

```bash
sudo chown root:lp \
  /opt/ringcentral-fax/process_print_job.py \
  /opt/ringcentral-fax/send_fax.py \
  /opt/ringcentral-fax/fax_worker.py

sudo chmod 750 \
  /opt/ringcentral-fax/process_print_job.py \
  /opt/ringcentral-fax/send_fax.py \
  /opt/ringcentral-fax/fax_worker.py
```

Avoid `chmod 777` on application files.

For administrative editing, use `sudoedit`, an editor through `sudo`, or an ACL for the administrator account.

Example ACL:

```bash
sudo setfacl -m u:ADMINUSER:rwx /opt/ringcentral-fax

sudo setfacl -m u:ADMINUSER:rw \
  /opt/ringcentral-fax/process_print_job.py \
  /opt/ringcentral-fax/send_fax.py \
  /opt/ringcentral-fax/fax_worker.py

sudo setfacl -m d:u:ADMINUSER:rwX /opt/ringcentral-fax
```

Replace `ADMINUSER` with the appropriate administrator account.

---

## 3. Create the Python Environment

```bash
sudo python3 -m venv /opt/ringcentral-fax/venv

sudo /opt/ringcentral-fax/venv/bin/pip install --upgrade pip

sudo /opt/ringcentral-fax/venv/bin/pip install \
  -r requirements.txt
```

The processor requires `pypdf`. If it is not already in `requirements.txt`:

```bash
sudo /opt/ringcentral-fax/venv/bin/pip install pypdf
```

---

## 4. Install GhostPDL

SAP sends PJL/PCL, so the gateway requires GhostPDL with PCL support.

The tested version is:

```text
GhostPDL 10.08.0
```

The tested installation procedure:

```bash
cd /tmp

wget \
  https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10080/ghostpdl-10.08.0.tar.gz

tar -xzf ghostpdl-10.08.0.tar.gz

cd ghostpdl-10.08.0

./configure

make
```

Install the compiled `gpdl` executable:

```bash
sudo install -d -o root -g root -m 755 \
  /opt/ringcentral-fax/bin

sudo install -o root -g root -m 755 \
  /tmp/ghostpdl-10.08.0/bin/gpdl \
  /opt/ringcentral-fax/bin/gpdl
```

Verify:

```bash
sudo -u lp /opt/ringcentral-fax/bin/gpdl --version
```

Check linked libraries:

```bash
ldd /opt/ringcentral-fax/bin/gpdl | grep 'not found'
```

No output from the `ldd` command indicates that no linked libraries are missing.

Do not depend on the build under `/tmp`; copy the working executable into the permanent application tree.

---

## 5. Configure RingCentral

Copy the environment file:

```bash
sudo cp .env.example /opt/ringcentral-fax/.env
sudo vi /opt/ringcentral-fax/.env
```

The current Python code expects variables similar to:

```text
RC_CLIENT_ID=replace_me
RC_CLIENT_SECRET=replace_me
RC_JWT_TOKEN=replace_me
RC_SERVER=https://platform.ringcentral.com
```

Protect the credentials:

```bash
sudo chown root:lp /opt/ringcentral-fax/.env
sudo chmod 640 /opt/ringcentral-fax/.env
```

Verify that the worker account can read the file:

```bash
sudo -u lp test -r /opt/ringcentral-fax/.env \
  && echo "ENV readable" \
  || echo "ENV NOT readable"
```

Never commit `.env` or production credentials to Git.

---

## 6. Create the Fax Queue Directories

```bash
sudo mkdir -p \
  /var/spool/ringcentral-fax/pending \
  /var/spool/ringcentral-fax/processing \
  /var/spool/ringcentral-fax/completed \
  /var/spool/ringcentral-fax/failed \
  /var/spool/ringcentral-fax/artifacts
```

Set ownership and permissions:

```bash
sudo chown -R lp:lp \
  /var/spool/ringcentral-fax/pending \
  /var/spool/ringcentral-fax/processing \
  /var/spool/ringcentral-fax/completed \
  /var/spool/ringcentral-fax/failed \
  /var/spool/ringcentral-fax/artifacts

sudo chmod 750 \
  /var/spool/ringcentral-fax/pending \
  /var/spool/ringcentral-fax/processing \
  /var/spool/ringcentral-fax/completed \
  /var/spool/ringcentral-fax/failed \
  /var/spool/ringcentral-fax/artifacts
```

Configure SELinux:

```bash
sudo semanage fcontext -a -t print_spool_t \
  '/var/spool/ringcentral-fax(/.*)?'

sudo restorecon -Rv /var/spool/ringcentral-fax
```

Verify:

```bash
sudo ls -ldZ \
  /var/spool/ringcentral-fax/{pending,processing,completed,failed,artifacts}
```

The SELinux type should be:

```text
print_spool_t
```

Verify `lp` can write:

```bash
sudo -u lp touch \
  /var/spool/ringcentral-fax/pending/test-write

sudo -u lp rm \
  /var/spool/ringcentral-fax/pending/test-write
```

---

## 7. Install the CUPS Backend

Install the asynchronous `sapfax` backend:

```bash
sudo install -o root -g root -m 755 \
  sapfax \
  /usr/lib/cups/backend/sapfax
```

Restore its SELinux context:

```bash
sudo restorecon -v /usr/lib/cups/backend/sapfax
```

Syntax check:

```bash
sudo bash -n /usr/lib/cups/backend/sapfax
```

Verify that CUPS can query the backend:

```bash
sudo -u lp /usr/lib/cups/backend/sapfax
```

Expected:

```text
direct sapfax "Unknown" "SAP RingCentral Fax Queue Backend"
```

---

## 8. Create the CUPS Queue

Production example:

```bash
sudo lpadmin \
  -p sap_rfax \
  -E \
  -v sapfax:/ \
  -m raw
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

Testing environments may use a queue such as:

```text
sap_rfax_test
```

Substitute the actual queue name in all CUPS commands.

---

## 9. Install the Persistent Worker Service

Install the service:

```bash
sudo cp ringcentral-fax-worker.service \
  /etc/systemd/system/ringcentral-fax-worker.service
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

Enable and start the worker:

```bash
sudo systemctl enable --now ringcentral-fax-worker
```

Verify:

```bash
systemctl status ringcentral-fax-worker
```

Expected:

```text
Active: active (running)
```

Watch the worker:

```bash
sudo journalctl -u ringcentral-fax-worker -f
```

At startup the worker should authenticate once:

```text
Creating RingCentral SDK session...
Authenticating to RingCentral...
RingCentral authentication successful.
```

---

# Example systemd Service

`ringcentral-fax-worker.service`:

```ini
[Unit]
Description=RingCentral Fax Queue Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lp
Group=lp
WorkingDirectory=/opt/ringcentral-fax

ExecStart=/opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/fax_worker.py --send

Restart=on-failure
RestartSec=5

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

# Firewall

LPD uses TCP/515.

For temporary testing:

```bash
sudo firewall-cmd --add-port=515/tcp
```

For production, restrict access to the authorized SAP/Linux print server:

```bash
sudo firewall-cmd --permanent \
  --add-rich-rule='rule family="ipv4" source address="192.0.2.10/32" port port="515" protocol="tcp" accept'

sudo firewall-cmd --reload
```

Replace `192.0.2.10` with the actual source print-server address.

---

# Spool Retention and Cleanup

The gateway uses **systemd-tmpfiles** for automatic spool cleanup.

Configuration:

```text
/etc/tmpfiles.d/ringcentral-fax.conf
```

Tested rule:

```text
# Remove RingCentral fax spool files older than 7 days
d /var/spool/ringcentral-fax 0750 lp lp -
e /var/spool/ringcentral-fax - - - 7d
```

Verify:

```bash
sudo cat /etc/tmpfiles.d/ringcentral-fax.conf
```

Check the cleanup timer:

```bash
systemctl status systemd-tmpfiles-clean.timer
systemctl list-timers systemd-tmpfiles-clean.timer
```

Manual cleanup:

```bash
sudo systemd-tmpfiles --clean \
  /etc/tmpfiles.d/ringcentral-fax.conf
```

## Important queue-retention consideration

With the asynchronous worker design, do **not** silently delete jobs that remain in `pending/` or `processing/` merely because they are old.

For production, retention should preferentially target completed artifacts and explicitly handled failed jobs. Pending jobs should remain visible until processed or intentionally investigated/removed.

Review the tmpfiles policy if queue subdirectories are retained for long periods.

---

# Testing

## Test 1 — Standalone RingCentral API

Use a known PDF:

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/send_fax.py \
  --to 555-555-5555 \
  --file /path/to/test_fax.pdf \
  --cover 0 \
  --wait
```

This verifies credentials, network access, and basic RingCentral fax API functionality.

---

## Test 2 — PCL Processor Dry Run

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/process_print_job.py \
  --file /path/to/captured-job.raw \
  --no-send
```

Expected output includes:

```text
RightFax routing metadata detected.
Converting PCL to PDF...
Rendered PDF: ...
Removing RightFax routing page...
Fax PDF: ...
DRY RUN SUCCESS
```

---

## Test 3 — Worker Dry Run

Copy a known job into `pending/`:

```bash
sudo cp /path/to/captured-job.raw \
  /var/spool/ringcentral-fax/pending/test-worker.raw

sudo chown lp:lp \
  /var/spool/ringcentral-fax/pending/test-worker.raw
```

Process one job without sending:

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/fax_worker.py \
  --once
```

The original job should move:

```text
pending/
  ↓
processing/
  ↓
completed/
```

---

## Test 4 — One Live Worker Fax

Place one safe test job in `pending/`, then:

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/fax_worker.py \
  --once \
  --send
```

Expected RingCentral API behavior:

```text
POST /restapi/oauth/token       200
POST /restapi/.../fax           200
```

---

## Test 5 — Persistent Session / Bulk Worker Test

Queue several safe test jobs and run:

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/fax_worker.py \
  --send
```

The worker should authenticate once and send all queued jobs using the same session.

A validated six-job test produced:

```text
1 OAuth token request
6 successful fax submissions
0 Auth 429 errors
```

---

## Test 6 — Full SAP → CUPS → Worker Path

Watch CUPS:

```bash
sudo journalctl -u cups -f
```

Watch the worker:

```bash
sudo journalctl -u ringcentral-fax-worker -f
```

Have SAP send one controlled fax job.

Expected behavior:

```text
SAP
 ↓
LPD/515
 ↓
CUPS
 ↓
sapfax
 ↓
pending/
 ↓
CUPS marks print job complete
 ↓
worker claims job
 ↓
processing/
 ↓
PCL → PDF
 ↓
RingCentral
 ↓
completed/
```

The CUPS queue should clear independently of the RingCentral processing time.

---

# Monitoring

## Worker Status

```bash
systemctl status ringcentral-fax-worker
```

## Worker Logs

```bash
sudo journalctl -u ringcentral-fax-worker -f
```

Recent worker activity:

```bash
sudo journalctl \
  -u ringcentral-fax-worker \
  --since "30 minutes ago" \
  --no-pager
```

## CUPS Status

```bash
systemctl is-active cups
systemctl is-active cups-lpd.socket

lpstat -v sap_rfax
lpstat -p sap_rfax -l
lpstat -o sap_rfax
```

## Queue Directories

```bash
sudo find /var/spool/ringcentral-fax \
  -maxdepth 2 \
  -type f \
  \( -path '*/pending/*' \
     -o -path '*/processing/*' \
     -o -path '*/completed/*' \
     -o -path '*/failed/*' \) \
  -ls
```

Quick interactive view:

```bash
watch -n 1 '
echo "=== PENDING ==="
ls -1 /var/spool/ringcentral-fax/pending 2>/dev/null
echo
echo "=== PROCESSING ==="
ls -1 /var/spool/ringcentral-fax/processing 2>/dev/null
echo
echo "=== COMPLETED ==="
ls -1 /var/spool/ringcentral-fax/completed 2>/dev/null | tail -10
echo
echo "=== FAILED ==="
ls -1 /var/spool/ringcentral-fax/failed 2>/dev/null
'
```

---

# Troubleshooting

## Worker Is Not Running

```bash
systemctl status ringcentral-fax-worker
sudo journalctl -u ringcentral-fax-worker -n 100 --no-pager
```

Restart:

```bash
sudo systemctl restart ringcentral-fax-worker
```

---

## Job Is Stuck in `pending/`

Verify the worker is active:

```bash
systemctl is-active ringcentral-fax-worker
```

Check permissions:

```bash
sudo ls -ldZ \
  /var/spool/ringcentral-fax/pending \
  /var/spool/ringcentral-fax/processing
```

Test worker access:

```bash
sudo -u lp test -r \
  /var/spool/ringcentral-fax/pending/<job>.raw \
  && echo readable
```

---

## Job Is in `failed/`

Review the worker log:

```bash
sudo journalctl \
  -u ringcentral-fax-worker \
  --since "30 minutes ago" \
  --no-pager
```

Inspect metadata:

```bash
sudo strings /var/spool/ringcentral-fax/failed/<job>.raw |
grep -E '\{\{(fax|contact|owner|winsecid|billing|notifyhost)'
```

Dry-run the processor:

```bash
sudo -u lp \
  /opt/ringcentral-fax/venv/bin/python \
  /opt/ringcentral-fax/process_print_job.py \
  --file /var/spool/ringcentral-fax/failed/<job>.raw \
  --no-send
```

---

## RingCentral HTTP 429

The persistent worker should prevent repeated OAuth authentication during normal batches.

If the RingCentral API still returns `429`, inspect the worker log and RingCentral developer-console request history.

The worker is designed to identify common rate-limit indicators, wait before retrying, and pace normal fax submissions.

Do not repeatedly restart the worker during an active penalty interval unless necessary, because a restart causes a new authentication request.

---

## CUPS Queue Disabled

With the asynchronous backend, RingCentral/API failures should no longer disable CUPS.

If the CUPS printer itself becomes disabled:

```bash
lpstat -p sap_rfax -l
lpstat -o sap_rfax
```

After correcting the underlying CUPS/backend problem:

```bash
sudo cupsenable sap_rfax
sudo cupsaccept sap_rfax
```

Do not automatically cancel queued jobs unless they are known duplicates or should not be transmitted.

---

## Nothing Reaches TCP/515

```bash
sudo ss -lntp | grep ':515'
sudo firewall-cmd --list-all
sudo tcpdump -nni any 'tcp port 515'
```

If no traffic reaches TCP/515, troubleshoot the upstream print server, routing, network ACLs, or host firewall.

---

## Inspect an Original SAP Job

```bash
sudo file /path/to/job.raw
sudo xxd -l 512 /path/to/job.raw
```

Look for:

```text
@PJL ENTER LANGUAGE=PCL
```

Metadata can be inspected with:

```bash
sudo strings /path/to/job.raw |
grep -E '\{\{(fax|contact|owner|winsecid|billing|notifyhost)'
```

---

## GhostPDL Problems

```bash
sudo -u lp /opt/ringcentral-fax/bin/gpdl --version
ldd /opt/ringcentral-fax/bin/gpdl | grep 'not found'
namei -l /opt/ringcentral-fax/bin/gpdl
```

Check SELinux:

```bash
sudo ausearch -m AVC,USER_AVC -ts recent
```

Keep SELinux enforcing and troubleshoot the actual denial rather than permanently disabling SELinux.

---

# CUPS Compatibility

The gateway currently depends on:

- `cups-lpd`
- a CUPS raw queue
- a traditional custom backend

The validated CUPS build is:

```text
CUPS 2.3.3op2-39.el9_8
```

These are legacy CUPS mechanisms, so major CUPS upgrades should be tested before deployment.

## Optional CUPS Version Hold

Under the existing `[main]` section of:

```text
/etc/dnf/dnf.conf
```

add:

```ini
# CUPS held at known-working version for SAP -> RingCentral fax gateway.
# Validate the complete fax pipeline before allowing CUPS upgrades.
excludepkgs=cups*
```

Record the known-good package set:

```bash
rpm -qa | grep '^cups' | sort |
sudo tee /opt/ringcentral-fax/cups-known-good-versions.txt
```

Before removing the hold, validate:

```text
SAP
 ↓
LPD/515
 ↓
CUPS
 ↓
sapfax
 ↓
pending/
 ↓
worker
 ↓
PCL → PDF
 ↓
RingCentral
```

## Long-Term CUPS Migration Path

If a future CUPS release removes raw queues or traditional backends, the intended migration is:

```text
SAP
 ↓ LPD/TCP 515
dedicated LPD receiver
 ↓
pending/
 ↓
ringcentral-fax-worker
 ↓
process_print_job.py
 ↓
GhostPDL
 ↓
RingCentral
```

Because the fax processing and RingCentral submission are already separated from CUPS, replacing the CUPS/LPD ingestion layer should not require redesigning the worker or document-processing pipeline.

---

# Security

For production:

- Restrict TCP/515 to authorized source systems.
- Keep SELinux enforcing.
- Protect `/opt/ringcentral-fax/.env`.
- Never commit JWTs, client secrets, or `.env`.
- Run the worker as `lp`, not root.
- Keep application files non-world-writable.
- Treat raw PCL files and generated PDFs as sensitive business documents.
- Review failed jobs rather than silently discarding them.
- Define retention appropriate for purchase-order data.
- Monitor the worker service and failed queue.
- Validate CUPS compatibility before major upgrades.
- Rotate RingCentral credentials according to organizational policy.

---

# `.gitignore`

Recommended:

```gitignore
.env
venv/
.venv/
__pycache__/
*.py[cod]
*.log
*.raw
*.pcl
*.pdf
```

Do not commit production fax documents, raw spool captures, RingCentral credentials, JWTs, client secrets, or other sensitive data.
