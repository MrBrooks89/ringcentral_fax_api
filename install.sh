#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/ringcentral-fax"
SPOOL_DIR="/var/spool/ringcentral-fax"
BACKEND="/usr/lib/cups/backend/sapfax"
QUEUE="${QUEUE:-sap_rfax}"
OPEN_FIREWALL="${OPEN_FIREWALL:-no}"
PIN_CUPS="${PIN_CUPS:-no}"
INSTALL_GHOSTPDL="${INSTALL_GHOSTPDL:-yes}"
SPOOL_RETENTION_DAYS="${SPOOL_RETENTION_DAYS:-7}"
TMPFILES_CONF="/etc/tmpfiles.d/ringcentral-fax.conf"
GHOSTPDL_VERSION="10.08.0"
GHOSTPDL_TAG="gs10080"
GHOSTPDL_TARBALL="ghostpdl-${GHOSTPDL_VERSION}.tar.gz"
GHOSTPDL_URL="https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/${GHOSTPDL_TAG}/${GHOSTPDL_TARBALL}"
GHOSTPDL_BUILD_DIR="/tmp/ghostpdl-${GHOSTPDL_VERSION}"
GPDL_BIN="$APP_DIR/bin/gpdl"


die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "Run this installer as root (sudo ./install.sh)."

[[ "$SPOOL_RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] || die "SPOOL_RETENTION_DAYS must be a positive integer."

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
    policycoreutils-python-utils \
    wget \
    tar \
    gcc \
    gcc-c++ \
    make

info "Creating application and spool directories..."
install -d -o root -g root -m 755 "$APP_DIR"
install -d -o root -g root -m 755 "$APP_DIR/bin"
install -d -o lp -g lp -m 750 "$SPOOL_DIR"

info "Configuring spool retention with systemd-tmpfiles (${SPOOL_RETENTION_DAYS} days)..."
cat > "$TMPFILES_CONF" <<EOF
# Remove RingCentral fax spool files older than ${SPOOL_RETENTION_DAYS} days
d ${SPOOL_DIR} 0750 lp lp -
e ${SPOOL_DIR} - - - ${SPOOL_RETENTION_DAYS}d
EOF
chmod 644 "$TMPFILES_CONF"
systemd-tmpfiles --create "$TMPFILES_CONF"
systemctl enable --now systemd-tmpfiles-clean.timer >/dev/null 2>&1 || true

info "Installing Python application..."
install -o root -g lp -m 750 "$REPO_DIR/process_print_job.py" "$APP_DIR/process_print_job.py"
install -o root -g lp -m 750 "$REPO_DIR/send_fax.py" "$APP_DIR/send_fax.py"
install -o root -g root -m 644 "$REPO_DIR/requirements.txt" "$APP_DIR/requirements.txt"

info "Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if ! "$APP_DIR/venv/bin/python" -c 'import pypdf' >/dev/null 2>&1; then
    info "Installing pypdf..."
    "$APP_DIR/venv/bin/pip" install pypdf
fi

if [[ "$INSTALL_GHOSTPDL" == "yes" ]]; then
    info "Building GhostPDL ${GHOSTPDL_VERSION} with PCL support..."
    rm -rf "$GHOSTPDL_BUILD_DIR"
    cd /tmp
    wget -O "$GHOSTPDL_TARBALL" "$GHOSTPDL_URL"
    tar -xzf "$GHOSTPDL_TARBALL"
    cd "$GHOSTPDL_BUILD_DIR"
    ./configure
    make
    [[ -x "$GHOSTPDL_BUILD_DIR/bin/gpdl" ]] || die "GhostPDL build completed but bin/gpdl was not found."
    install -o root -g root -m 755 "$GHOSTPDL_BUILD_DIR/bin/gpdl" "$GPDL_BIN"
else
    info "Skipping GhostPDL build because INSTALL_GHOSTPDL=$INSTALL_GHOSTPDL"
fi

[[ -x "$GPDL_BIN" ]] || die "Missing executable GhostPDL binary: $GPDL_BIN"

info "Verifying GhostPDL as lp..."
su -s /bin/sh lp -c "'$GPDL_BIN' --version >/dev/null"
if ldd "$GPDL_BIN" | grep -q 'not found'; then
    ldd "$GPDL_BIN" | grep 'not found' >&2 || true
    die "GhostPDL has missing shared-library dependencies."
fi

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

if [[ "$PIN_CUPS" == "yes" ]]; then
    info "Adding CUPS package hold to /etc/dnf/dnf.conf..."
    if grep -Eq '^[[:space:]]*excludepkgs=.*(^|[[:space:],])cups\*' /etc/dnf/dnf.conf 2>/dev/null; then
        info "CUPS exclusion already present."
    else
        if grep -q '^\[main\]' /etc/dnf/dnf.conf; then
            sed -i '/^\[main\]/a excludepkgs=cups*' /etc/dnf/dnf.conf
        else
            printf '\n[main]\nexcludepkgs=cups*\n' >> /etc/dnf/dnf.conf
        fi
    fi
    rpm -qa | grep '^cups' | sort > "$APP_DIR/cups-known-good-versions.txt"
else
    echo
    echo "CUPS packages were NOT pinned."
    echo "To add excludepkgs=cups* automatically, run:"
    echo "  sudo PIN_CUPS=yes ./install.sh"
fi

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
"$GPDL_BIN" --version || true
echo
echo "=== Spool Retention ==="
cat "$TMPFILES_CONF" || true
systemctl --no-pager --full status systemd-tmpfiles-clean.timer 2>/dev/null | head -8 || true

echo
echo "Installation complete."
echo
echo "Next steps:"
echo "  1. Configure $APP_DIR/.env"
echo "  2. Confirm RingCentral API with a known PDF"
echo "  3. Dry-run a captured SAP PCL file as user lp"
echo "  4. Test an end-to-end SAP/CUPS job with RingCentral sending disabled"
echo "  5. Re-enable RingCentral sending for a controlled live fax"
echo "  6. Verify spool cleanup: systemctl status systemd-tmpfiles-clean.timer"
echo "  6. Configure a source-restricted TCP/515 firewall rule"
echo "  7. Configure spool retention/cleanup"
