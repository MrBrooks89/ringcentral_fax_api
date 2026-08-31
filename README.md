# RingCentral Fax API Gateway

A Linux fax gateway that receives print jobs over **LPD (TCP/515)**, processes the incoming document with **CUPS**, converts the document to PDF, and submits the fax through the **RingCentral Fax API** using Python.

## What This Accomplishes

This project provides a bridge between a traditional SAP/Linux print workflow and RingCentral faxing.

```text
┌─────────┐
│   SAP   │
└────┬────┘
     │ internal SAP spool processing
     ▼
┌────────────────────┐
│ Local Print Queue  │ 
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
### Queue Naming

There are two seperate print queues in this architecture.

**Local SAP host queue**

This queue exists on the Linux host servicing the SAP spool system. SAP submits the print job to this local queue using it's configured output device.

Example:

    dc1_rc1

The local queue is configured to forward jobs using LPR/LPD to the RingCentral Fax Gateway.

**Gateway LPD queue**

This queue exists on teh RingCentral Fax Gateway and is exposed through the cups-lpd.

Example:

    sap_rfax

The upstream Linux queue therefore users:

    Remote host: <RINGCENTRAL_GATEWAY>
    Protocol: LPR/LPD
    Port: TCP 515
    Remote queue: sap_rfax

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

<ServerBin>/backend/sapfax
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

The supplied `install.sh` is a single, rerunnable orchestrator for the exact
supported IDs below. It adapts package commands by OS family and refuses to
guess unsupported or unsafe host capabilities.

Run it from the repository directory:

```bash
chmod +x install.sh
sudo ./install.sh --queue sap_rfax
```

## Supported Hosts and Adapters

| Exact `/etc/os-release` ID | Family | Package manager | Packages |
|---|---|---|---|
| `rhel`, `fedora`, `centos`, `rocky`, `almalinux` | RHEL | `dnf` | `cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils` |
| `opensuse-leap`, `opensuse-tumbleweed`, `sles` | SUSE | `zypper` | `cups python3 python3-pip firewalld apparmor-utils` |

Support is matched by exact `ID`; `ID_LIKE` is not used. SUSE intentionally
does not request a separate `cups-lpd` package. Full installation requires
SELinux enforcing on RHEL-family hosts and an enabled AppArmor interface with
working `aa-status` on SUSE-family hosts.

## Preflight and Runtime Options

Use `./install.sh --check` for non-root, read-only inspection. It never creates
a virtual environment or changes packages, files, services, firewall rules,
CUPS, SELinux, or AppArmor. Exit status `0` means ready, `2` means supported
but installable prerequisites are missing, and `1` means unsupported distro,
malformed input, unsafe configuration, or a non-remediable capability failure.
For a selected firewall zone, non-root `--check` uses a running firewalld
daemon when available or readable installed zone definitions; if neither is
available, it reports that membership cannot be verified rather than declaring
the zone invalid.

The full command is:

```text
sudo ./install.sh [--queue NAME] [--firewall-zone ZONE] [--allow-cidr CIDR]
```

`FAX_QUEUE` (default `sap_rfax`), `FAX_FIREWALL_ZONE`, and
`FAX_ALLOWED_CIDR` provide environment defaults; CLI values take precedence.
The firewall zone and CIDR must be supplied together. CIDRs are strict,
canonicalized IPv4/IPv6 networks, and only one source network is accepted per
run. `OPEN_FIREWALL` is rejected; rerun with another source CIDR when another
authorized host is required.

These installer settings are runtime inputs only; they are deliberately not
part of `.env.example`, which remains exclusively the four-key RingCentral
application schema.

Failures identify the active phase and completed operations. The installer does
not claim rollback: package, service, queue, LSM, or firewall changes already
completed remain in place and are reported for the operator to review.

The installer:

1. Installs the exact family-specific package set from the supported-host matrix.
2. After capability verification, disables and stops `cups-lpd.socket` before
   deployment continues, including on reruns.
3. Creates `/opt/ringcentral-fax`.
4. Creates `/var/spool/ringcentral-fax`.
5. Creates the Python virtual environment.
6. Installs `requirements.txt`.
7. Installs `process_print_job.py` and `send_fax.py`.
8. Installs the custom `sapfax` CUPS backend.
9. Configures the spool directory and verifies the expected LSM handling.
10. Enables CUPS.
11. Creates the CUPS fax queue.
12. Configures and verifies an optional installer-managed, source-restricted
    TCP/515 rule, then activates `cups-lpd.socket` only for that managed path.
    With no firewall inputs it leaves the socket disabled and prints the staged
    next step for an operator using an external control.

The installer intentionally does **not** create production RingCentral credentials.

## Manual Installation

### 1. Install Packages

The installer selects the package adapter from the exact ID matrix above. For
reference, RHEL-family uses `dnf -y install ...`; SUSE uses
`zypper --non-interactive install --no-recommends ...`. Do not substitute a
different package set without revalidating the capability gates.

```bash
sudo dnf install -y cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils
```

On SUSE-family hosts, the equivalent adapter is:

```bash
sudo zypper --non-interactive install --no-recommends cups python3 python3-pip firewalld apparmor-utils
```

Start CUPS so the raw queue can be created:

```bash
sudo systemctl enable --now cups
```

### 2. Install the Application

```bash
sudo mkdir -p /opt/ringcentral-fax
sudo cp process_print_job.py send_fax.py /opt/ringcentral-fax/
```

Create the Python environment:

```bash
sudo /usr/bin/python3 -I -m venv /opt/ringcentral-fax/venv
sudo /opt/ringcentral-fax/venv/bin/python -I -m pip --isolated install --upgrade pip
sudo /opt/ringcentral-fax/venv/bin/python -I -m pip --isolated install -r requirements.txt
```

### 3. Configure RingCentral

Create the production credential file with its final restrictive ownership and
mode before opening it in an editor:

```bash
sudo install -o root -g lp -m 640 .env.example /opt/ringcentral-fax/.env
sudo vi /opt/ringcentral-fax/.env
```

Populate the values required by `send_fax.py`.

`.env.example` is a committed, blank configuration schema. The production
`/opt/ringcentral-fax/.env` file is deployment-specific secret configuration
and must never be committed. `send_fax.py` deterministically loads the `.env`
file adjacent to the installed script; for the production installation that is
`/opt/ringcentral-fax/.env`. Values already supplied in the process environment
take precedence over values in that file.

The schema has exactly these four keys:

```text
RC_CLIENT_ID=
RC_CLIENT_SECRET=
RC_JWT_TOKEN=
RC_SERVER=
```

Set `RC_SERVER` to the RingCentral server/environment selected by the operator
for this deployment (for example, the approved production or sandbox endpoint).
Do not hard-code a server choice in source control. Existing deployments using
the former `RC_JWT` name must migrate it to `RC_JWT_TOKEN`; `RC_JWT` is not
read by the application.

On installation, an existing `.env` is never overwritten: its bytes and
RingCentral values are preserved. The installer rejects symlinks and
non-regular files, then repairs and verifies ownership `root:lp` and mode
`0640`. A missing file is created from the blank schema only.

The CUPS backend normally executes as the `lp` account, so the production
credential file must be owned by `root:lp` with mode `0640`:

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

On RHEL-family hosts, configure the SELinux file context:

```bash
sudo semanage fcontext -a -t print_spool_t '/var/spool/ringcentral-fax(/.*)?'

sudo restorecon -Rv /var/spool/ringcentral-fax
```

Verify:

```bash
ls -Zd /var/spool/ringcentral-fax
```

The SELinux type should be `print_spool_t`.

On SUSE-family hosts, keep AppArmor enabled and inspect its state with
`aa-status`; the installer does not generate or disable profiles. A real CUPS
job may expose a release/site-specific denial requiring a narrowly scoped local
profile adjustment.

### 5. Install the CUPS Backend

```bash
sudo install -o root -g root -m 755 sapfax <ServerBin>/backend/sapfax
```

`<ServerBin>` is discovered from an active `ServerBin` directive in
`/etc/cups/cups-files.conf`, then `cups-config --serverbin`, then the package
file list. If discovery is missing, ambiguous, non-absolute, or unsafe, the
installer stops; it never creates a guessed system-library backend path.

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

The installer capability-gates this requirement by running `lpadmin ... -m raw`
and stops with an actionable incompatibility error if CUPS rejects it; it never
silently selects a transforming filter.

### 7. Allow LPD Through a Source-Restricted Firewall Rule

LPD uses TCP/515.

The installer only accepts one validated source CIDR and creates one
idempotent rich rule per invocation. For a documentation-only example, use a
reserved network:

```bash
sudo ./install.sh --firewall-zone public --allow-cidr 192.0.2.10/32
```

The installer validates zone membership before starting firewalld and uses
`firewall-offline-cmd` to query permanent broad exposure and the desired rich
rule even when the daemon is inactive. It starts firewalld only after those
checks, reloads only when a permanent rule was added or the runtime rule is
absent, and verifies both permanent and runtime state. It refuses to add a
source-restricted rule to a zone that already exposes TCP/515 or the `lpd`
service broadly. It never opens TCP/515 broadly.

If you omit both firewall options, the installer intentionally makes no
firewall change, stops and disables `cups-lpd.socket`, and reports a staged
installation. It does not claim to have secured TCP/515. An external firewall
or network control must first be verified to restrict LPD access to authorized
print servers; that operator must then separately enable the socket.

For a manual source-restricted setup, first make sure no already-broad
permanent port or service exposure exists. The following uses only a reserved
documentation CIDR; replace it only with an authorized source approved for the
deployment.

```bash
ZONE=public
CIDR=192.0.2.10/32
RULE="rule family=\"ipv4\" source address=\"${CIDR}\" port port=\"515\" protocol=\"tcp\" accept"

offline_query() {
  sudo firewall-offline-cmd --zone="${ZONE}" "$@"
  status=$?
  case "${status}" in
    0|1) return "${status}" ;;
    *) echo "firewall-offline-cmd query failed (status ${status})" >&2; return "${status}" ;;
  esac
}

if offline_query --query-port=515/tcp; then
  echo "Refusing: ${ZONE} already exposes TCP/515 broadly" >&2
  exit 1
else
  status=$?
  [[ "${status}" == 1 ]] || exit "${status}"
fi
if offline_query --query-service=lpd; then
  echo "Refusing: ${ZONE} already exposes LPD broadly" >&2
  exit 1
else
  status=$?
  [[ "${status}" == 1 ]] || exit "${status}"
fi

if offline_query --query-rich-rule "${RULE}"; then
  :
else
  status=$?
  [[ "${status}" == 1 ]] || exit "${status}"
  sudo firewall-offline-cmd --zone="${ZONE}" --add-rich-rule "${RULE}"
fi

sudo systemctl enable --now firewalld
sudo firewall-cmd --reload
sudo firewall-offline-cmd --zone="${ZONE}" --query-rich-rule "${RULE}"
sudo firewall-cmd --zone "${ZONE}" --query-rich-rule "${RULE}"

sudo systemctl enable --now cups-lpd.socket
sudo ss -lntp | grep ':515'
```

Stop if any query fails unexpectedly rather than treating it as absence. This
installer-managed rule path activates `cups-lpd.socket` only after permanent
and runtime verification succeeds. An already-verified external firewall or
network control that restricts TCP/515 to authorized print servers is the
alternative; its operator must separately run `sudo systemctl enable --now
cups-lpd.socket` and verify TCP/515 is listening.

---

### 8. Configure Spool Cleanup

The fax gateway stores temporary print-stream and PDF files under:

    /var/spool/ringcentral-fax

These files are useful for troubleshooting but should not be retained
indefinitely. Without automatic cleanup, the spool directory can eventually
consume all available disk space.

Configure `systemd-tmpfiles` to remove spool files older than 3 days.

Create:

    /etc/tmpfiles.d/ringcentral-fax.conf

With the following contents:

```text
# RingCentral Fax Gateway spool directory
d /var/spool/ringcentral-fax 0750 lp lp -

# Remove files from the spool directory after 3 days
e /var/spool/ringcentral-fax - - - 3d
```
To test cleanup method manually:
```bash
sudo systemd-tmpfiles --clean --prefix=/var/spool/ringcentral-fax
```
# Testing

Run the hermetic installer tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The tests source installer functions in isolated child shells, use temporary
fixtures and fake commands, and record mutating argv without executing it.
Those internal helper tests do not add a root bypass or production-path option:
production `main` always reads its production paths and requires root for a
full installation.

`--check` is the only installer mode suitable for ordinary host inspection.
Full installation must be validated manually on disposable RHEL-family and
SUSE-family hosts after package, CUPS, LSM, and firewalld capabilities are
confirmed. Do not authenticate to RingCentral or send a fax for this validation.

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
YYYYMMDD-HHMMSS.XXXXXX-job.raw
YYYYMMDD-HHMMSS.raw
YYYYMMDD-HHMMSS.pdf
```

## Test 4 — Remote LPD

From the Linux host providing the SAP locoal print queue, submit an LPR job to the gateway.

The destination should be: 

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
```

Do not enable `cups-lpd.socket` merely to troubleshoot. If the installer was
run without firewall inputs, it intentionally leaves the socket disabled.
First verify either the installer-managed source-restricted rich rule from the
manual firewall flow above or an external control restricts TCP/515 to
authorized print servers. The managed installer path enables the socket after
that verification; for an external-control path, its operator may then
separately enable the socket and verify it is listening:

```bash
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

The `YYYYMMDD-HHMMSS.XXXXXX-job.raw` file is the original stream received by
the backend. The six-character segment is generated uniquely for each capture.

```bash
sudo ls -ltr /var/spool/ringcentral-fax
sudo less /var/spool/ringcentral-fax/<timestamp>.<random>-job.raw
```

For text data:

```bash
sudo cat /var/spool/ringcentral-fax/<timestamp>.<random>-job.raw
```

This is the first place to look if SAP changes the print format or fax metadata is no longer parsed correctly.

## PDF Is Not Created

If `YYYYMMDD-HHMMSS.XXXXXX-job.raw` exists but no `.pdf` is created, test the
processor directly:

```bash
cat /var/spool/ringcentral-fax/<timestamp>.<random>-job.raw | sudo -u lp /opt/ringcentral-fax/venv/bin/python /opt/ringcentral-fax/process_print_job.py
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

## AppArmor on SUSE

The installer requires the AppArmor kernel interface and a working
`aa-status`. It does not disable AppArmor, generate broad local profiles, or
alter global policy. After a real CUPS job, inspect site-specific denials with:

```bash
sudo aa-status
sudo journalctl -k --since "10 minutes ago" | grep -i apparmor
```

If a denial is confirmed, have the site administrator make the narrowest local
profile adjustment for that release; do not use `aa-disable` as a workaround.

## `Exec format error`

If CUPS reports:

```text
execv failed: Exec format error
```

verify the backend:

```bash
head -1 <ServerBin>/backend/sapfax
file <ServerBin>/backend/sapfax
ls -l <ServerBin>/backend/sapfax
```

The script needs a valid shebang, such as:

```bash
#!/bin/bash
```

and must be executable:

```bash
sudo chmod 755 <ServerBin>/backend/sapfax
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
 cups-lpd     Was YYYYMMDD-HHMMSS.XXXXXX-job.raw created?
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
- Monitor disabled CUPS queues and failed API submissions.
- Rotate API credentials according to organizational policy.
- Never commit production or business data: `.env` files; client IDs, client
  secrets, JWTs, or server credentials; real fax numbers, contacts, owners,
  WinSecIDs, billing values, or notification hosts; customer documents,
  print streams, PDFs, or unsanitized spool files; RingCentral message IDs;
  or production CUPS, network, and host configuration details.

---
