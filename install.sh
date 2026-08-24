#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/ringcentral-fax"
SPOOL_DIR="/var/spool/ringcentral-fax"
BACKEND="/usr/lib/cups/backend/sapfax"
QUEUE="${QUEUE:-sap_rfax}"
OPEN_FIREWALL="${OPEN_FIREWALL:-no}"

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "Run this installer as root (sudo ./install.sh)."

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in process_print_job.py send_fax.py sapfax requirements.txt; do
    [[ -f "$REPO_DIR/$f" ]] || die "Missing required repository file: $f"
done

command -v dnf >/dev/null 2>&1 || die "This installer currently supports RHEL/Fedora-compatible dnf systems."

info "Installing OS packages..."
dnf install -y \
    cups \
    cups-lpd \
    python3 \
    python3-pip \
    firewalld \
    policycoreutils-python-utils

info "Creating application and spool directories..."
install -d -o root -g root -m 755 "$APP_DIR"
install -d -o lp -g lp -m 750 "$SPOOL_DIR"

info "Installing Python application..."
install -o root -g root -m 644 "$REPO_DIR/process_print_job.py" "$APP_DIR/process_print_job.py"
install -o root -g root -m 644 "$REPO_DIR/send_fax.py" "$APP_DIR/send_fax.py"
install -o root -g root -m 644 "$REPO_DIR/requirements.txt" "$APP_DIR/requirements.txt"

info "Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

info "Installing CUPS backend..."
install -o root -g root -m 755 "$REPO_DIR/sapfax" "$BACKEND"

if command -v semanage >/dev/null 2>&1; then
    info "Configuring SELinux context for fax spool..."
    semanage fcontext -a -t print_spool_t "${SPOOL_DIR}(/.*)?" 2>/dev/null || \
        semanage fcontext -m -t print_spool_t "${SPOOL_DIR}(/.*)?"
    restorecon -Rv "$SPOOL_DIR"
else
    echo "WARNING: semanage not available; SELinux spool context was not configured."
fi

info "Enabling CUPS..."
systemctl enable --now cups

info "Enabling cups-lpd socket..."
systemctl enable --now cups-lpd.socket

info "Creating/updating CUPS queue: $QUEUE"
lpadmin -p "$QUEUE" -E -v sapfax:/ -m raw
cupsaccept "$QUEUE"
cupsenable "$QUEUE"

if [[ "$OPEN_FIREWALL" == "yes" ]]; then
    info "Opening TCP/515 in firewalld..."
    systemctl enable --now firewalld
    firewall-cmd --permanent --add-port=515/tcp
    firewall-cmd --reload
    echo "WARNING: TCP/515 is open broadly. Restrict it to authorized print-server IPs for production."
else
    echo
    echo "Firewall was NOT modified."
    echo "For a temporary test:"
    echo "  sudo firewall-cmd --add-port=515/tcp"
    echo
    echo "For production, use a source-restricted rich rule."
    echo "To let this installer open 515 broadly, run:"
    echo "  sudo OPEN_FIREWALL=yes ./install.sh"
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
    if [[ -f "$REPO_DIR/.env.example" ]]; then
        info "Installing .env.example as $APP_DIR/.env"
        install -o root -g lp -m 640 "$REPO_DIR/.env.example" "$APP_DIR/.env"
        echo "IMPORTANT: Edit $APP_DIR/.env and add valid RingCentral credentials."
    else
        echo "WARNING: $APP_DIR/.env does not exist. Create it before sending faxes."
    fi
else
    info "Existing $APP_DIR/.env preserved."
fi

echo
echo "=== Installation Summary ==="
systemctl --no-pager --full status cups-lpd.socket 2>/dev/null | head -8 || true
echo
ss -lnt | grep ':515' || echo "WARNING: Nothing currently shown listening on TCP/515."
echo
lpstat -v "$QUEUE" || true
lpstat -p "$QUEUE" -l || true

echo
echo "Installation complete."
echo
echo "Next steps:"
echo "  1. Configure $APP_DIR/.env"
echo "  2. Test RingCentral API directly"
echo "  3. Test the processor as user lp"
echo "  4. Test locally: lp -d $QUEUE test_sap_fax.txt"
echo "  5. Configure a source-restricted TCP/515 firewall rule"
