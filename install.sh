#!/bin/bash
# Cross-distribution installer for the RingCentral fax gateway.

APP_DIR="/opt/ringcentral-fax"
SPOOL_DIR="/var/spool/ringcentral-fax"
ENV_FILE="${APP_DIR}/.env"
QUEUE="sap_rfax"
FIREWALL_ZONE=""
ALLOW_CIDR=""
CHECK_ONLY=0
PHASE="startup"
COMPLETED="none"
DISTRO_ID=""
DISTRO_VERSION=""
DISTRO_FAMILY=""
PACKAGE_MANAGER=""
BACKEND_DIR=""
BACKEND_TARGET=""
CIDR_CANONICAL=""
CIDR_FAMILY=""
PREFLIGHT_STATUS=0
VENV_PYTHON=""
LPD_ACTIVATION_ALLOWED=0
ACTIVE_LSM=""

RHEL_IDS=(rhel fedora centos rocky almalinux)
SUSE_IDS=(opensuse-leap opensuse-tumbleweed sles)
RHEL_PACKAGES=(cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils)
SUSE_BASE_CAPABILITIES=(cups python3 python3-pip firewalld)
SUSE_SELINUX_CAPABILITIES=(/usr/sbin/getenforce /usr/sbin/semanage /usr/sbin/restorecon /usr/sbin/matchpathcon)
SUSE_APPARMOR_CAPABILITIES=(/usr/sbin/aa-status)

info() { printf '%s\n' "$*"; }
phase_fail() {
    printf 'ERROR [phase=%s; completed=%s]: %s\n' "$PHASE" "$COMPLETED" "$*" >&2
    return 1
}
run_cmd() { "$@"; }
have_command() { command -v "$1" >/dev/null 2>&1; }
mark_complete() {
    if [[ "$COMPLETED" == none ]]; then COMPLETED="$1"; else COMPLETED="$COMPLETED,$1"; fi
}

usage() {
    cat <<'EOF'
Usage: install.sh [--check] [--queue NAME] [--firewall-zone ZONE] [--allow-cidr CIDR]

--check performs read-only preflight without requiring root. Exit 0 means ready,
2 means supported but installable prerequisites are missing, and 1 means an
unsupported distro, malformed input, unsafe configuration, or fatal capability.
Environment: FAX_QUEUE, FAX_FIREWALL_ZONE, FAX_ALLOWED_CIDR. CLI values win.
Full installation activates cups-lpd.socket only after an installer-managed,
source-restricted firewall rule is verified. Without both firewall inputs, it
leaves the socket disabled for an operator to enable after verifying an
external access control.
EOF
}

validate_queue() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$ ]] || phase_fail "invalid CUPS queue name" || return 1
}
validate_zone_syntax() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$ ]] || phase_fail "invalid firewall zone name" || return 1
}
canonicalize_cidr() {
    local result
    have_command python3 || { phase_fail "python3 is required to validate CIDR"; return 1; }
    [[ "$1" == */* ]] || { phase_fail "allow-cidr must include a CIDR prefix length"; return 1; }
    result="$(python3 -I -c 'import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=True); print(n.version, n.with_prefixlen)' "$1" 2>/dev/null)" || {
        phase_fail "allow-cidr must be canonical CIDR notation"; return 1;
    }
    CIDR_FAMILY="${result%% *}"
    CIDR_CANONICAL="${result#* }"
}

parse_args() {
    PHASE="input"
    [[ ! -v OPEN_FIREWALL ]] || { phase_fail "OPEN_FIREWALL is unsupported; use --firewall-zone with --allow-cidr"; return 1; }
    QUEUE="${FAX_QUEUE:-sap_rfax}"
    FIREWALL_ZONE="${FAX_FIREWALL_ZONE:-}"
    ALLOW_CIDR="${FAX_ALLOWED_CIDR:-}"
    while (( $# )); do
        case "$1" in
            --help|-h) usage; return 2 ;;
            --check) CHECK_ONLY=1 ;;
            --queue) (( $# >= 2 )) || { phase_fail "--queue requires a value"; return 1; }; QUEUE="$2"; shift ;;
            --firewall-zone) (( $# >= 2 )) || { phase_fail "--firewall-zone requires a value"; return 1; }; FIREWALL_ZONE="$2"; shift ;;
            --allow-cidr) (( $# >= 2 )) || { phase_fail "--allow-cidr requires a value"; return 1; }; ALLOW_CIDR="$2"; shift ;;
            *) phase_fail "unknown option: $1"; return 1 ;;
        esac
        shift
    done
    validate_queue "$QUEUE" || return 1
    if [[ -n "$FIREWALL_ZONE" || -n "$ALLOW_CIDR" ]]; then
        [[ -n "$FIREWALL_ZONE" && -n "$ALLOW_CIDR" ]] || { phase_fail "firewall zone and allow-cidr must be supplied together"; return 1; }
        validate_zone_syntax "$FIREWALL_ZONE" || return 1
        canonicalize_cidr "$ALLOW_CIDR" || return 1
    fi
}

read_os_release() {
    local file="${1:-/etc/os-release}" line key value
    [[ -r "$file" ]] || { phase_fail "cannot read os-release: $file"; return 1; }
    DISTRO_ID=""; DISTRO_VERSION=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
        if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
        case "$key" in ID) DISTRO_ID="$value" ;; VERSION_ID) DISTRO_VERSION="$value" ;; esac
    done < "$file"
    [[ "$DISTRO_ID" =~ ^[a-z0-9][a-z0-9-]*$ && -n "$DISTRO_VERSION" ]] || { phase_fail "malformed os-release"; return 1; }
}

selinux_interface_enabled() { [[ -d /sys/fs/selinux ]]; }
apparmor_interface_enabled() { [[ -d /sys/kernel/security/apparmor ]]; }
select_suse_packages() {
    PACKAGES=("${SUSE_BASE_CAPABILITIES[@]}")
    if selinux_interface_enabled; then
        PACKAGES+=("${SUSE_SELINUX_CAPABILITIES[@]}")
    elif apparmor_interface_enabled; then
        PACKAGES+=("${SUSE_APPARMOR_CAPABILITIES[@]}")
    fi
}

detect_distro() {
    PHASE="distro detection"
    read_os_release "${1:-/etc/os-release}" || return 1
    DISTRO_FAMILY=""; PACKAGE_MANAGER=""; PACKAGES=()
    case "$DISTRO_ID" in
        rhel|fedora|centos|rocky|almalinux) DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=("${RHEL_PACKAGES[@]}") ;;
        opensuse-leap|opensuse-tumbleweed|sles) DISTRO_FAMILY=SUSE; PACKAGE_MANAGER=zypper; select_suse_packages ;;
        *) phase_fail "unsupported distro ID: $DISTRO_ID"; return 1 ;;
    esac
}

repo_files_ok() {
    local f
    for f in process_print_job.py send_fax.py sapfax requirements.txt .env.example; do
        [[ -f "$REPO_DIR/$f" ]] || { phase_fail "required repository file is missing: $f"; return 1; }
    done
}

unit_present() { systemctl list-unit-files "$1" --no-legend 2>/dev/null | grep -Fq "$1"; }
unit_state() { local enabled active; enabled="$(systemctl is-enabled "$1" 2>/dev/null || echo unknown)"; active="$(systemctl is-active "$1" 2>/dev/null || echo inactive)"; info "$1: enabled=$enabled active=$active"; }
check_lsm() {
    local mode=""
    PHASE="LSM preflight"
    ACTIVE_LSM=""
    if [[ "$DISTRO_FAMILY" == RHEL ]]; then
        if ! have_command getenforce; then info "SELinux: missing getenforce (installable prerequisite)"; return 2; fi
        mode="$(getenforce 2>/dev/null || true)"
        info "SELinux: ${mode:-unavailable}"
        [[ "$mode" == Enforcing ]] || { phase_fail "RHEL full installation requires SELinux enforcing; remediate without disabling the LSM"; return 1; }
        ACTIVE_LSM=SELINUX
        return 0
    fi

    # Leap/SLE 16 default to SELinux, while earlier or switched SUSE releases
    # can use AppArmor. Select from the active kernel interface, not OS version.
    if selinux_interface_enabled; then
        if ! have_command getenforce; then info "SELinux: missing getenforce (installable prerequisite)"; return 2; fi
        mode="$(getenforce 2>/dev/null || true)"
        info "SELinux: ${mode:-unavailable}"
        case "$mode" in
            Enforcing) ACTIVE_LSM=SELINUX; return 0 ;;
            Permissive) phase_fail "SUSE SELinux is active but not enforcing; remediate without disabling the LSM"; return 1 ;;
            Disabled|"") phase_fail "SUSE SELinux kernel interface is active but enforcement state is unavailable"; return 1 ;;
            *) phase_fail "could not determine SUSE SELinux enforcement state"; return 1 ;;
        esac
    fi

    if apparmor_interface_enabled; then
        if ! have_command aa-status; then info "AppArmor: missing aa-status (installable prerequisite)"; return 2; fi
        aa-status --enabled >/dev/null 2>&1 || { phase_fail "SUSE AppArmor kernel interface is present but AppArmor is not enabled; no policy changes were attempted"; return 1; }
        ACTIVE_LSM=APPARMOR
        info "AppArmor: enabled (site profiles remain unchanged)"
        if have_command systemctl; then
            local aa_enabled aa_active; aa_enabled="$(systemctl is-enabled apparmor.service 2>/dev/null || echo unknown)"; aa_active="$(systemctl is-active apparmor.service 2>/dev/null || echo inactive)"; info "AppArmor service: enabled=$aa_enabled active=$aa_active"
        fi
        aa-status 2>/dev/null | grep -E 'profiles are|processes are' || true
        return 0
    fi

    phase_fail "SUSE full installation requires an active SELinux or AppArmor kernel interface; no LSM changes were attempted"
    return 1
}

selinux_config_mode() {
    local file="${1:-/etc/selinux/config}" line mode="" count=0
    [[ -r "$file" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        if [[ "$line" =~ ^[[:space:]]*SELINUX[[:space:]]*=[[:space:]]*([A-Za-z]+)[[:space:]]*$ ]]; then
            mode="${BASH_REMATCH[1],,}"
            (( count += 1 ))
        fi
    done < "$file"
    [[ "$count" == 1 ]] || return 1
    printf '%s\n' "$mode"
}

verify_lsm_persistence() {
    local configured enabled
    case "$ACTIVE_LSM" in
        SELINUX)
            configured="$(selinux_config_mode 2>/dev/null || true)"
            [[ "$configured" == enforcing ]] || { phase_fail "SELinux must be configured as enforcing in /etc/selinux/config for reboot persistence"; return 1; }
            ;;
        APPARMOR)
            enabled="$(systemctl is-enabled apparmor.service 2>/dev/null || true)"
            [[ "$enabled" == enabled ]] || { phase_fail "AppArmor service must be enabled for reboot persistence (found ${enabled:-unavailable})"; return 1; }
            ;;
        *) phase_fail "active LSM was not established for persistence verification"; return 1 ;;
    esac
}

verify_active_lsm_enforcement() {
    local mode
    case "$ACTIVE_LSM" in
        SELINUX)
            mode="$(getenforce 2>/dev/null || true)"
            [[ "$mode" == Enforcing ]] || { phase_fail "SELinux enforcement changed after preflight (found ${mode:-unavailable})"; return 1; }
            ;;
        APPARMOR)
            apparmor_interface_enabled && aa-status --enabled >/dev/null 2>&1 || { phase_fail "AppArmor enforcement changed after preflight"; return 1; }
            ;;
        *) phase_fail "active LSM was not established for enforcement verification"; return 1 ;;
    esac
}

record_preflight_status() {
    # A fatal result always wins over an installable missing prerequisite.
    # Do not let a later status 2 downgrade an earlier status 1.
    local candidate="$1"
    if [[ "$candidate" == 1 ]]; then
        PREFLIGHT_STATUS=1
    elif [[ "$candidate" == 2 && "$PREFLIGHT_STATUS" != 1 ]]; then
        PREFLIGHT_STATUS=2
    fi
}

validate_existing_directory() {
    local path="$1" description="$2"
    if [[ -L "$path" ]]; then
        phase_fail "$description must not be a symlink"
        return 1
    fi
    if [[ -e "$path" && ! -d "$path" ]]; then
        phase_fail "$description must be a directory when it already exists"
        return 1
    fi
}

validate_deployment_paths() {
    validate_existing_directory "$APP_DIR" "application directory" || return 1
    validate_existing_directory "$SPOOL_DIR" "spool directory" || return 1
    validate_existing_directory "$APP_DIR/venv" "application virtual environment" || return 1
}

path_is_root_owned_and_not_writable() {
    local path="$1" description="$2" owner mode
    owner="$(stat -c %u -- "$path" 2>/dev/null || true)"
    [[ "$owner" == 0 ]] || { phase_fail "$description is not root-owned: $path"; return 1; }
    if [[ ! -L "$path" ]]; then
        mode="$(stat -c %a -- "$path" 2>/dev/null || true)"
        [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 8#022) == 0 )) || {
            phase_fail "$description is group/world writable: $path"
            return 1
        }
    fi
}

path_is_within() {
    local path="$1" parent="$2"
    [[ "$path" == "$parent" || "$path" == "$parent/"* ]]
}

verify_venv_tree() {
    local venv="$APP_DIR/venv" venv_root system_python object resolved resolved_python
    [[ -d "$venv" && ! -L "$venv" ]] || { phase_fail "application virtual environment must be a non-symlink directory"; return 1; }
    path_is_root_owned_and_not_writable "$APP_DIR" "application directory" || return 1
    [[ -f "$venv/pyvenv.cfg" && ! -L "$venv/pyvenv.cfg" ]] || { phase_fail "application virtual environment is missing a regular pyvenv.cfg"; return 1; }
    [[ -d "$venv/bin" && ! -L "$venv/bin" ]] || { phase_fail "application virtual environment is missing a non-symlink bin directory"; return 1; }
    [[ -e "$venv/bin/python" || -L "$venv/bin/python" ]] || { phase_fail "application virtual environment is missing bin/python"; return 1; }
    venv_root="$(readlink -f -- "$venv" 2>/dev/null || true)"
    system_python="$(readlink -f -- "$(command -v python3)" 2>/dev/null || true)"
    [[ -n "$venv_root" && -n "$system_python" && -x "$system_python" ]] || { phase_fail "could not resolve the system Python interpreter for virtual environment verification"; return 1; }
    while IFS= read -r -d '' object; do
        path_is_root_owned_and_not_writable "$object" "virtual environment object" || return 1
        if [[ -L "$object" ]]; then
            resolved="$(readlink -f -- "$object" 2>/dev/null || true)"
            [[ -n "$resolved" && -e "$resolved" ]] || { phase_fail "virtual environment contains a broken symlink: $object"; return 1; }
            if ! path_is_within "$resolved" "$venv_root" && [[ "$resolved" != "$system_python" ]]; then
                phase_fail "virtual environment symlink escapes its tree: $object"
                return 1
            fi
        fi
    done < <(find -P "$venv" -xdev -print0)
    resolved_python="$(readlink -f -- "$venv/bin/python" 2>/dev/null || true)"
    [[ "$resolved_python" == "$system_python" && -f "$resolved_python" ]] || { phase_fail "virtual environment interpreter does not resolve to the trusted system Python"; return 1; }
    path_is_root_owned_and_not_writable "$resolved_python" "resolved virtual environment interpreter" || return 1
    VENV_PYTHON="$venv/bin/python"
    [[ -x "$VENV_PYTHON" ]] || { phase_fail "virtual environment interpreter is not executable"; return 1; }
}

discover_backend_dir() {
    local config="${1:-/etc/cups/cups-files.conf}" sb line candidate count=0
    local -a candidates=()
    add_candidate() { local existing; for existing in "${candidates[@]}"; do [[ "$existing" == "$1" ]] && return; done; candidates+=("$1"); }
    PHASE="CUPS backend discovery"
    if [[ -r "$config" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" =~ ^[[:space:]]*ServerBin[[:space:]]+([^[:space:]#]+) ]] || continue
            add_candidate "${BASH_REMATCH[1]}"
        done < "$config"
    fi
    if (( ${#candidates[@]} == 0 )) && have_command cups-config; then
        sb="$(cups-config --serverbin 2>/dev/null || true)"; [[ -n "$sb" ]] && add_candidate "$sb"
    fi
    if (( ${#candidates[@]} == 0 )) && have_command rpm; then
        while IFS= read -r line; do [[ "$line" == */cups/backend ]] && add_candidate "${line%/backend}"; done < <(rpm -ql cups 2>/dev/null)
    fi
    if (( ${#candidates[@]} == 0 )); then
        phase_fail "CUPS ServerBin discovery is unavailable; the package capability is missing"
        return 2
    fi
    (( ${#candidates[@]} == 1 )) || { phase_fail "CUPS ServerBin discovery is ambiguous; no backend directory was guessed"; return 1; }
    sb="${candidates[0]}"
    [[ "$sb" = /* ]] || { phase_fail "CUPS ServerBin is not absolute"; return 1; }
    BACKEND_DIR="$(readlink -f -- "${sb}/backend" 2>/dev/null || true)"
    [[ -d "$BACKEND_DIR" ]] || { phase_fail "CUPS backend directory does not exist: ${sb}/backend"; return 2; }
    local owner mode
    owner="$(stat -c %u -- "$BACKEND_DIR" 2>/dev/null || true)"; mode="$(stat -c %a -- "$BACKEND_DIR" 2>/dev/null || true)"
    [[ "$owner" == 0 ]] || { phase_fail "CUPS backend directory is not root-owned"; return 1; }
    [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 8#022) == 0 )) || { phase_fail "CUPS backend directory is group/world writable"; return 1; }
    BACKEND_TARGET="${BACKEND_DIR}/sapfax"
    [[ ! -L "$BACKEND_TARGET" ]] || { phase_fail "CUPS backend target is a symlink"; return 1; }
    info "CUPS backend directory: $BACKEND_DIR"
}

preflight() {
    local c
    PREFLIGHT_STATUS=0
    PHASE="preflight"
    detect_distro || return 1
    info "Distro: $DISTRO_ID $DISTRO_VERSION ($DISTRO_FAMILY), package manager $PACKAGE_MANAGER"
    repo_files_ok || return 1
    if ! have_command "$PACKAGE_MANAGER"; then
        phase_fail "required package manager is unavailable: $PACKAGE_MANAGER; installation cannot bootstrap this supported host"
        record_preflight_status 1
    fi
    for c in systemctl getent python3 lpadmin lpstat cupsaccept cupsenable; do have_command "$c" || { info "Missing command: $c (installable prerequisite)"; record_preflight_status 2; }; done
    if have_command systemctl; then
        for c in cups.service cups-lpd.socket; do
            if unit_present "$c"; then unit_state "$c"; else info "Missing unit: $c (installable prerequisite)"; record_preflight_status 2; fi
        done
    fi
    getent passwd lp >/dev/null 2>&1 || { info "Missing lp user (installable prerequisite)"; record_preflight_status 2; }
    getent group lp >/dev/null 2>&1 || { info "Missing lp group (installable prerequisite)"; record_preflight_status 2; }
    if have_command python3; then info "Python: $(python3 -I --version 2>&1)"; python3 -I -c 'import venv,ensurepip' >/dev/null 2>&1 || { info "Python venv/ensurepip unavailable"; record_preflight_status 2; }; fi
    if discover_backend_dir; then :; else c=$?; record_preflight_status "$c"; fi
    if check_lsm; then
        verify_lsm_persistence || record_preflight_status 1
    else
        c=$?
        record_preflight_status "$c"
    fi
    if ! have_command firewall-cmd; then info "Missing firewall-cmd (installable prerequisite)"; record_preflight_status 2; else
        local fwstate fwenabled; fwstate="$(firewall-cmd --state 2>/dev/null || echo inactive)"; fwenabled="$(systemctl is-enabled firewalld 2>/dev/null || echo unknown)"; info "Firewalld: state=$fwstate enabled=$fwenabled"
        if [[ -n "$FIREWALL_ZONE" ]]; then
            info "Firewalld selected zone: $FIREWALL_ZONE"
            validate_zone_membership || record_preflight_status 1
            if [[ "$fwstate" == running ]]; then
                local desired_rule="rule family=\"ipv${CIDR_FAMILY}\" source address=\"${CIDR_CANONICAL}\" port port=\"515\" protocol=\"tcp\" accept"
                firewall-cmd --permanent --zone "$FIREWALL_ZONE" --query-rich-rule "$desired_rule" >/dev/null 2>&1 && info "Desired permanent rich rule: present" || info "Desired permanent rich rule: absent"
            else info "Desired rich-rule status unavailable while firewalld is inactive"; fi
        fi
    fi
    if [[ -d "$APP_DIR" ]]; then info "Application path: present"; else info "Application path: absent"; fi
    if [[ -d "$SPOOL_DIR" ]]; then info "Spool path: present"; else info "Spool path: absent"; fi
    if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
        [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || { phase_fail "existing .env is not a regular non-symlink file"; record_preflight_status 1; }
        info ".env metadata: $(stat -c '%U:%G %a' -- "$ENV_FILE" 2>/dev/null || echo unavailable)"
    else info ".env: absent (created during installation)"; fi
    validate_deployment_paths || record_preflight_status 1
    return "$PREFLIGHT_STATUS"
}

install_packages() {
    PHASE="package installation"
    if [[ "$PACKAGE_MANAGER" == dnf ]]; then run_cmd dnf -y install "${PACKAGES[@]}" || { phase_fail "dnf failed for $DISTRO_ID packages: ${PACKAGES[*]}; required capabilities remain missing"; return 1; }; else run_cmd zypper --non-interactive install --no-recommends --capability "${PACKAGES[@]}" || { phase_fail "zypper failed for $DISTRO_ID capabilities: ${PACKAGES[*]}; required capabilities remain missing"; return 1; }; fi
    mark_complete packages
}

post_package_verify() {
    PHASE="post-package capability verification"
    local c
    for c in systemctl getent python3 lpadmin lpstat cupsaccept cupsenable firewall-cmd firewall-offline-cmd; do have_command "$c" || { phase_fail "missing post-package capability: $c"; return 1; }; done
    check_lsm || return 1
    PHASE="post-package capability verification"
    if [[ "$ACTIVE_LSM" == SELINUX ]]; then
        for c in getenforce semanage restorecon matchpathcon; do have_command "$c" || { phase_fail "missing post-package capability: $c"; return 1; }; done
    elif [[ "$ACTIVE_LSM" == APPARMOR ]]; then
        have_command aa-status || { phase_fail "missing post-package capability: aa-status"; return 1; }
    else
        phase_fail "active LSM was not established"
        return 1
    fi
    verify_lsm_persistence || return 1
    unit_present cups.service || { phase_fail "cups.service is missing after package installation"; return 1; }
    unit_present cups-lpd.socket || { phase_fail "cups-lpd.socket is missing after package installation"; return 1; }
    getent passwd lp >/dev/null 2>&1 && getent group lp >/dev/null 2>&1 || { phase_fail "lp identity/group missing after package installation"; return 1; }
    python3 -I -c 'import venv,ensurepip' >/dev/null 2>&1 || { phase_fail "python venv support missing after package installation"; return 1; }
    discover_backend_dir || return 1
    mark_complete capabilities
}

stage_lpd_socket() {
    PHASE="LPD socket staging"
    LPD_ACTIVATION_ALLOWED=0
    run_cmd systemctl disable cups-lpd.socket || { phase_fail "cups-lpd.socket disablement failed"; return 1; }
    run_cmd systemctl stop cups-lpd.socket || { phase_fail "cups-lpd.socket stop failed"; return 1; }
    mark_complete lpd_staged
}

create_dirs() {
    PHASE="directory setup"
    validate_deployment_paths || return 1
    run_cmd install -d -o root -g root -m 755 "$APP_DIR" || { phase_fail "application directory setup failed"; return 1; }
    run_cmd install -d -o lp -g lp -m 750 "$SPOOL_DIR" || { phase_fail "spool directory setup failed"; return 1; }
    mark_complete directories
}
install_application() {
    PHASE="application installation"
    local f; for f in process_print_job.py send_fax.py requirements.txt; do run_cmd install -o root -g root -m 644 "$REPO_DIR/$f" "$APP_DIR/$f" || { phase_fail "failed installing $f"; return 1; }; done
    mark_complete application
}
create_venv() {
    PHASE="Python environment"
    validate_existing_directory "$APP_DIR/venv" "application virtual environment" || return 1
    if [[ -e "$APP_DIR/venv" || -L "$APP_DIR/venv" ]]; then
        verify_venv_tree || return 1
    else
        run_cmd python3 -I -m venv "$APP_DIR/venv" || { phase_fail "venv creation failed"; return 1; }
        verify_venv_tree || return 1
    fi
    "$VENV_PYTHON" -I -m pip --isolated --version >/dev/null 2>&1 || { phase_fail "venv pip capability missing"; return 1; }
    run_cmd "$VENV_PYTHON" -I -m pip --isolated install -r "$APP_DIR/requirements.txt" || { phase_fail "dependency installation failed"; return 1; }
    mark_complete venv
}
verify_env() {
    local file="$1" owner mode
    [[ -f "$file" && ! -L "$file" ]] || { phase_fail ".env must be a regular non-symlink file"; return 1; }
    owner="$(stat -c %U:%G -- "$file" 2>/dev/null || true)"; mode="$(stat -c %a -- "$file" 2>/dev/null || true)"
    [[ "$owner" == root:lp && "$mode" == 640 ]] || { phase_fail ".env ownership/mode verification failed (expected root:lp 0640)"; return 1; }
}
ensure_env() {
    PHASE="credential file"
    if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || { phase_fail "existing .env is not a regular non-symlink file"; return 1; }; else run_cmd install -o root -g lp -m 640 "$REPO_DIR/.env.example" "$ENV_FILE" || { phase_fail "failed creating .env schema"; return 1; }; fi
    run_cmd chown root:lp -- "$ENV_FILE" || { phase_fail "failed repairing .env ownership"; return 1; }
    run_cmd chmod 0640 -- "$ENV_FILE" || { phase_fail "failed repairing .env mode"; return 1; }
    verify_env "$ENV_FILE" || return 1
    mark_complete credentials
}
install_backend() {
    PHASE="backend installation"
    validate_backend_target || return 1
    run_cmd install -o root -g root -m 755 "$REPO_DIR/sapfax" "$BACKEND_TARGET" || { phase_fail "backend installation failed"; return 1; }
    verify_backend_target || return 1
    mark_complete backend
}

validate_backend_target() {
    [[ -n "$BACKEND_TARGET" && ! -L "$BACKEND_TARGET" ]] || { phase_fail "validated backend target is unavailable"; return 1; }
    if [[ -e "$BACKEND_TARGET" && ! -f "$BACKEND_TARGET" ]]; then
        phase_fail "existing backend target must be a regular file: $BACKEND_TARGET"
        return 1
    fi
}

verify_backend_target() {
    local owner mode
    [[ -f "$BACKEND_TARGET" && ! -L "$BACKEND_TARGET" ]] || { phase_fail "installed backend is not a regular non-symlink file"; return 1; }
    owner="$(stat -c %u -- "$BACKEND_TARGET" 2>/dev/null || true)"
    mode="$(stat -c %a -- "$BACKEND_TARGET" 2>/dev/null || true)"
    [[ "$owner" == 0 && "$mode" == 755 ]] || { phase_fail "installed backend metadata verification failed (expected root-owned mode 0755)"; return 1; }
}
configure_lsm() {
    PHASE="LSM configuration"
    verify_active_lsm_enforcement || return 1
    if [[ "$ACTIVE_LSM" == SELINUX ]]; then
        local contexts pattern
        pattern="${SPOOL_DIR}(/.*)?"
        contexts="$(semanage fcontext -l)" || { phase_fail "could not query SELinux file contexts"; return 1; }
        if awk -v pattern="$pattern" '$1 == pattern { found = 1 } END { exit !found }' <<<"$contexts"; then
            run_cmd semanage fcontext -m -t print_spool_t "$pattern" || { phase_fail "SELinux context update failed"; return 1; }
        else
            run_cmd semanage fcontext -a -t print_spool_t "$pattern" || { phase_fail "SELinux context creation failed"; return 1; }
        fi
        run_cmd restorecon -Rv "$SPOOL_DIR" "$BACKEND_TARGET" || { phase_fail "SELinux labeling failed"; return 1; }
        verify_selinux_label "$SPOOL_DIR" "print_spool_t" || return 1
        verify_selinux_default_label "$BACKEND_TARGET" || return 1
    elif [[ "$ACTIVE_LSM" == APPARMOR ]]; then
        aa-status --enabled >/dev/null 2>&1 || { phase_fail "AppArmor is not enabled; no policy was changed"; return 1; }
        info "AppArmor enabled; verify site-specific CUPS denials after deployment"
    else
        phase_fail "active LSM was not established before configuration"
        return 1
    fi
    mark_complete lsm
}

selinux_context_for() {
    local path="$1" context
    context="$(ls -Zd -- "$path" 2>/dev/null | awk 'NR == 1 { print $1 }')"
    [[ "$context" =~ ^[^:[:space:]]+:[^:[:space:]]+:[^:[:space:]]+:.+$ ]] || return 1
    printf '%s\n' "$context"
}
selinux_type_for() {
    local context
    context="$(selinux_context_for "$1")" || return 1
    local _user _role type _level
    IFS=: read -r _user _role type _level <<<"$context"
    printf '%s\n' "$type"
}
verify_selinux_label() {
    local path="$1" expected_type="$2" actual_type
    actual_type="$(selinux_type_for "$path")" || { phase_fail "could not read SELinux label for $path"; return 1; }
    [[ "$actual_type" == "$expected_type" ]] || { phase_fail "SELinux label verification failed for $path (expected type $expected_type, got $actual_type)"; return 1; }
}
verify_selinux_default_label() {
    local path="$1" actual expected
    actual="$(selinux_context_for "$path")" || { phase_fail "could not read SELinux label for $path"; return 1; }
    expected="$(matchpathcon -n "$path" 2>/dev/null | awk 'NR == 1 { print $1 }')"
    [[ "$expected" =~ ^[^:[:space:]]+:[^:[:space:]]+:[^:[:space:]]+:.+$ ]] || { phase_fail "could not determine expected SELinux label for $path"; return 1; }
    [[ "$actual" == "$expected" ]] || { phase_fail "SELinux label verification failed for $path (expected $expected, got $actual)"; return 1; }
}
configure_cups_service() {
    PHASE="CUPS service enablement"
    run_cmd systemctl enable --now cups || { phase_fail "cups enablement failed"; return 1; }
    mark_complete cups
}
configure_lpd_socket() {
    PHASE="LPD socket enablement"
    if [[ "$LPD_ACTIVATION_ALLOWED" != 1 ]]; then
        info "STAGED INSTALLATION: cups-lpd.socket remains disabled. Verify an external control that restricts TCP/515, then separately run: systemctl enable --now cups-lpd.socket"
        return 0
    fi
    run_cmd systemctl enable --now cups-lpd.socket || { phase_fail "cups-lpd.socket enablement failed"; return 1; }
    mark_complete lpd_socket
}
configure_queue() {
    PHASE="raw CUPS queue"
    run_cmd lpadmin -p "$QUEUE" -E -v sapfax:/ -m raw || { phase_fail "CUPS rejected the raw queue; raw-queue capability is required and no filter was selected"; return 1; }
    run_cmd cupsaccept "$QUEUE" || { phase_fail "cupsaccept failed"; return 1; }; run_cmd cupsenable "$QUEUE" || { phase_fail "cupsenable failed"; return 1; }
    lpstat -v "$QUEUE" 2>/dev/null | grep -Fq "sapfax:/" || { phase_fail "raw queue URI verification failed"; return 1; }
    mark_complete queue
}
is_root() { [[ "$EUID" -eq 0 ]]; }
firewall_zone_directories() { printf '%s\n' /etc/firewalld/zones /usr/lib/firewalld/zones /usr/share/firewalld/zones; }
readable_installed_zones() {
    local directory zone_file zone_name found=0
    while IFS= read -r directory; do
        [[ -d "$directory" && -r "$directory" ]] || continue
        for zone_file in "$directory"/*.xml; do
            [[ -r "$zone_file" ]] || continue
            zone_name="${zone_file##*/}"
            printf '%s\n' "${zone_name%.xml}"
            found=1
        done
    done < <(firewall_zone_directories)
    [[ "$found" == 1 ]]
}
known_zones() {
    if firewall-cmd --state 2>/dev/null | grep -Fxq running; then
        firewall-cmd --get-zones && return 0
    fi
    if is_root && have_command firewall-offline-cmd; then
        firewall-offline-cmd --get-zones && return 0
    fi
    readable_installed_zones
}
validate_zone_membership() {
    local zones status
    if zones="$(known_zones)"; then
        tr ' ' '\n' <<<"$zones" | grep -Fxq -- "$FIREWALL_ZONE" || { phase_fail "firewall zone is not installed: $FIREWALL_ZONE"; return 1; }
    else
        status=$?
        phase_fail "firewall zone membership cannot be verified with the current read-only capabilities"
        return "$status"
    fi
}
firewall_offline_query() {
    local status
    if firewall-offline-cmd --zone="$FIREWALL_ZONE" "$@" >/dev/null 2>&1; then
        return 0
    else
        status=$?
    fi
    [[ "$status" == 1 ]] && return 1
    phase_fail "permanent firewall query failed for $1 (status $status)"
    return 2
}
firewall_runtime_query() {
    local status
    if firewall-cmd --zone "$FIREWALL_ZONE" "$@" >/dev/null 2>&1; then
        return 0
    else
        status=$?
    fi
    [[ "$status" == 1 ]] && return 1
    phase_fail "runtime firewall query failed for $1 (status $status)"
    return 2
}
configure_firewall() {
    local rule permanent_present runtime_present added=0 status
    if [[ -z "$FIREWALL_ZONE" ]]; then
        LPD_ACTIVATION_ALLOWED=0
        info "STAGED INSTALLATION: installer made no TCP/515 firewall change and leaves cups-lpd.socket disabled. Verify an external control restricts LPD access, then separately enable the socket."
        return 0
    fi
    PHASE="firewall rule"
    validate_zone_membership || return 1
    if firewall_offline_query --query-port=515/tcp; then
        phase_fail "selected firewall zone already exposes TCP/515 broadly by port; remove it before using a source-restricted rule"
        return 1
    elif [[ "$?" != 1 ]]; then
        return 1
    fi
    if firewall_offline_query --query-service=lpd; then
        phase_fail "selected firewall zone already exposes LPD broadly by service; remove it before using a source-restricted rule"
        return 1
    elif [[ "$?" != 1 ]]; then
        return 1
    fi
    rule="rule family=\"ipv${CIDR_FAMILY}\" source address=\"${CIDR_CANONICAL}\" port port=\"515\" protocol=\"tcp\" accept"
    if firewall_offline_query --query-rich-rule "$rule"; then
        permanent_present=1
        info "Firewall permanent rule already present"
    else
        status=$?
        [[ "$status" == 1 ]] || return 1
        permanent_present=0
    fi
    run_cmd systemctl enable --now firewalld || { phase_fail "firewalld enablement failed"; return 1; }
    if firewall_runtime_query --query-port=515/tcp; then
        phase_fail "selected running firewall zone already exposes TCP/515 broadly by port; remove it before using a source-restricted rule"
        return 1
    elif [[ "$?" != 1 ]]; then
        return 1
    fi
    if firewall_runtime_query --query-service=lpd; then
        phase_fail "selected running firewall zone already exposes LPD broadly by service; remove it before using a source-restricted rule"
        return 1
    elif [[ "$?" != 1 ]]; then
        return 1
    fi
    if [[ "$permanent_present" == 0 ]]; then
        run_cmd firewall-cmd --permanent --zone "$FIREWALL_ZONE" --add-rich-rule "$rule" || { phase_fail "source-restricted firewall rule failed"; return 1; }
        added=1
    fi
    if firewall_runtime_query --query-rich-rule "$rule"; then
        runtime_present=1
    else
        status=$?
        [[ "$status" == 1 ]] || return 1
        runtime_present=0
    fi
    if [[ "$added" == 1 || "$runtime_present" == 0 ]]; then
        run_cmd firewall-cmd --reload || { phase_fail "firewalld reload failed"; return 1; }
    fi
    if firewall_offline_query --query-rich-rule "$rule"; then :; else phase_fail "permanent firewall rule verification failed"; return 1; fi
    if firewall_runtime_query --query-rich-rule "$rule"; then :; else phase_fail "runtime firewall rule verification failed"; return 1; fi
    LPD_ACTIVATION_ALLOWED=1
    mark_complete firewall
}
final_verify() {
    PHASE="final verification"
    verify_active_lsm_enforcement || return 1
    verify_lsm_persistence || return 1
    verify_env "$ENV_FILE" || return 1
    verify_backend_target || return 1
    systemctl is-active --quiet cups || { phase_fail "cups.service is not active"; return 1; }
    if [[ "$LPD_ACTIVATION_ALLOWED" == 1 ]]; then
        systemctl is-active --quiet cups-lpd.socket || { phase_fail "cups-lpd.socket is not active after verified firewall configuration"; return 1; }
    elif systemctl is-active --quiet cups-lpd.socket; then
        phase_fail "cups-lpd.socket must remain inactive until an external TCP/515 control is verified"
        return 1
    else
        info "Staged installation verified: cups-lpd.socket is inactive pending external TCP/515 access-control verification"
    fi
    lpstat -p "$QUEUE" >/dev/null 2>&1 || { phase_fail "CUPS queue verification failed"; return 1; }
    info "Installation complete: distro=$DISTRO_ID lsm=$ACTIVE_LSM queue=$QUEUE backend=$BACKEND_TARGET"
}

main() {
    PATH='/usr/sbin:/usr/bin:/sbin:/bin'
    export PATH
    hash -r
    unset BASH_ENV ENV
    local variable
    for variable in "${!LD_@}" "${!PYTHON@}" "${!PIP_@}"; do unset "$variable"; done
    set -euo pipefail
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if parse_args "$@"; then :; else
        local parse_status=$?
        [[ "$parse_status" == 2 ]] && return 0
        return "$parse_status"
    fi
    if ! detect_distro /etc/os-release; then return 1; fi
    local p=0
    if preflight; then p=0; else p=$?; fi
    if [[ "$CHECK_ONLY" == 1 ]]; then return "$p"; fi
    if [[ "$p" == 1 ]]; then return 1; fi
    if [[ "$p" == 2 ]]; then info "Preflight found installable prerequisites; continuing after package installation"; fi
    [[ "$CHECK_ONLY" == 1 ]] && return 0
    [[ "$EUID" -eq 0 ]] || { PHASE="privilege"; phase_fail "full installation requires root; use --check for non-mutating inspection"; return 1; }
    install_packages; post_package_verify; stage_lpd_socket; create_dirs; install_application; create_venv; ensure_env; install_backend; configure_lsm; configure_cups_service; configure_queue; configure_firewall; configure_lpd_socket; final_verify
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
