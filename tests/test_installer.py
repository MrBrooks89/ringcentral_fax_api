import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def bash(body, env=None):
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALLER))}\n{body}"],
        cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class InstallerTests(unittest.TestCase):
    def os_release(self, content):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "os-release"
        path.write_text(content)
        self.addCleanup(directory.cleanup)
        return path

    def fake_command(self, directory, name, body):
        command = Path(directory) / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
        return command

    def test_supported_distro_maps_and_packages(self):
        expected = {
            "rhel": ("RHEL", "dnf", "cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils"),
            "fedora": ("RHEL", "dnf", "cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils"),
            "centos": ("RHEL", "dnf", "cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils"),
            "rocky": ("RHEL", "dnf", "cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils"),
            "almalinux": ("RHEL", "dnf", "cups cups-lpd python3 python3-pip firewalld policycoreutils-python-utils"),
            "opensuse-leap": ("SUSE", "zypper", "cups python3 python3-pip firewalld apparmor-utils"),
            "opensuse-tumbleweed": ("SUSE", "zypper", "cups python3 python3-pip firewalld apparmor-utils"),
            "sles": ("SUSE", "zypper", "cups python3 python3-pip firewalld apparmor-utils"),
        }
        for distro, (family, manager, packages) in expected.items():
            with self.subTest(distro=distro):
                fixture = self.os_release(f'ID="{distro}"\nVERSION_ID="test"\n')
                result = bash(f'detect_distro {shlex.quote(str(fixture))}; printf "%s|%s|%s" "$DISTRO_FAMILY" "$PACKAGE_MANAGER" "${{PACKAGES[*]}}"')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{family}|{manager}|{packages}")

    def test_os_release_rejects_unsupported_missing_and_malformed(self):
        for content in ("ID=debian\nVERSION_ID=12\n", "VERSION_ID=9\n", "ID=RHEL\nVERSION_ID=9\n"):
            with self.subTest(content=content):
                self.assertEqual(bash(f'detect_distro {shlex.quote(str(self.os_release(content)))}').returncode, 1)

    def test_package_adapter_argv_is_exact(self):
        result = bash(r'''run_cmd() { printf '%s\n' "$*"; }
DISTRO_ID=rhel; PACKAGE_MANAGER=dnf; PACKAGES=(cups cups-lpd python3); install_packages
DISTRO_ID=sles; PACKAGE_MANAGER=zypper; PACKAGES=(cups python3); install_packages''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dnf -y install cups cups-lpd python3", result.stdout)
        self.assertIn("zypper --non-interactive install --no-recommends cups python3", result.stdout)
        self.assertNotIn("cups-lpd", result.stdout.split("zypper", 1)[1])

    def test_main_check_exit_codes_without_root_or_path_override(self):
        # Detection is faked so this CLI-path test is independent of the host ID;
        # it does not add a production path override or root bypass.
        for expected in (0, 1, 2):
            with self.subTest(expected=expected):
                result = bash(f'detect_distro() {{ DISTRO_ID=fedora; DISTRO_VERSION=fixture; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }}; preflight() {{ return {expected}; }}; main --check')
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_main_check_has_no_root_bypass_or_path_options(self):
        self.assertEqual(bash('detect_distro() { DISTRO_ID=fedora; DISTRO_VERSION=fixture; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }; preflight() { return 0; }; main --check').returncode, 0)
        source = INSTALLER.read_text()
        for forbidden in ("ROOT_BYPASS", "--os-release", "--app-dir", "--spool-dir"):
            self.assertNotIn(forbidden, source)

    def test_main_sanitizes_execution_environment_without_affecting_source_safety(self):
        result = bash(r'''BASH_ENV=fixture ENV=fixture LD_PRELOAD=fixture PYTHONPATH=fixture PIP_INDEX_URL=fixture
detect_distro() { DISTRO_ID=fedora; DISTRO_VERSION=fixture; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }
preflight() { printf '%s|%s|%s|%s|%s|%s' "${BASH_ENV-unset}" "${ENV-unset}" "${LD_PRELOAD-unset}" "${PYTHONPATH-unset}" "${PIP_INDEX_URL-unset}" "$PATH"; return 0; }
main --check''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "unset|unset|unset|unset|unset|/usr/sbin:/usr/bin:/sbin:/bin")
        source = INSTALLER.read_text()
        self.assertTrue(source.startswith("#!/bin/bash\n"))
        self.assertIn("hash -r", source)

    def test_cli_precedence_and_cidr_validation(self):
        env = os.environ | {"FAX_QUEUE": "from-env", "FAX_FIREWALL_ZONE": "public", "FAX_ALLOWED_CIDR": "192.0.2.0/24"}
        result = bash('parse_args --queue cli_queue --firewall-zone trusted --allow-cidr 2001:db8::/32; printf "%s|%s|%s|%s" "$QUEUE" "$FIREWALL_ZONE" "$CIDR_CANONICAL" "$CIDR_FAMILY"', env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "cli_queue|trusted|2001:db8::/32|6")
        for cidr, expected in (("192.0.2.0/24", "4|192.0.2.0/24"), ("2001:db8::/32", "6|2001:db8::/32")):
            result = bash(f'canonicalize_cidr {shlex.quote(cidr)}; printf "%s|%s" "$CIDR_FAMILY" "$CIDR_CANONICAL"')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
        invalid = ("192.0.2.1/24", "192.0.2.1", "300.1.1.1/24", "fe80::1%eth0/64", "192.0.2.0/24$(id)")
        for cidr in invalid:
            with self.subTest(cidr=cidr):
                self.assertEqual(bash(f"canonicalize_cidr {shlex.quote(cidr)}").returncode, 1)
        for args in ('--allow-cidr 192.0.2.1/24', '--firewall-zone public', '--firewall-zone "bad/zone" --allow-cidr 192.0.2.0/24', '--firewall-zone "public; touch /tmp/installer-injection" --allow-cidr 192.0.2.0/24', '--queue "bad queue"'):
            with self.subTest(args=args):
                self.assertEqual(bash(f"parse_args {args}").returncode, 1)
        self.assertEqual(bash("parse_args --check", env | {"OPEN_FIREWALL": "yes"}).returncode, 1)
        self.assertFalse(Path("/tmp/installer-injection").exists())

    def test_preflight_is_read_only_and_reports_missing_capabilities(self):
        result = bash(r'''detect_distro() { DISTRO_ID=fedora; DISTRO_VERSION=44; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }
repo_files_ok() { :; }
have_command() { case "$1" in lpadmin|cupsaccept) return 1;; *) return 0;; esac; }
unit_present() { return 1; }; unit_state() { :; }; getent() { return 1; }
python3() { case "$*" in *--version*) printf 'Python fixture\n'; return 0;; *-c*) return 1;; esac; }
discover_backend_dir() { return 2; }; check_lsm() { return 2; }
run_cmd() { printf 'MUTATION\n'; return 99; }; firewall-cmd() { printf 'inactive\n'; }
preflight; rc=$?; printf 'status=%s preflight=%s\n' "$rc" "$PREFLIGHT_STATUS"''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=2 preflight=2", result.stdout)
        for expected in ("Missing command: lpadmin", "Missing unit: cups.service", "Missing lp user", "Python venv/ensurepip unavailable"):
            self.assertIn(expected, result.stdout)
        self.assertNotIn("MUTATION", result.stdout)

    def test_preflight_missing_package_manager_is_fatal(self):
        result = bash(r'''detect_distro() { DISTRO_ID=fedora; DISTRO_VERSION=44; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }
repo_files_ok() { :; }; have_command() { [[ "$1" != dnf ]]; }; unit_present() { return 0; }; unit_state() { :; }; getent() { return 0; }; python3() { return 0; }
discover_backend_dir() { return 0; }; check_lsm() { return 0; }; firewall-cmd() { printf 'inactive\n'; }; systemctl() { printf 'enabled\n'; }
preflight; rc=$?; printf '%s|%s' "$rc" "$PREFLIGHT_STATUS"''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("1|1"), result.stdout)
        self.assertIn("installation cannot bootstrap", result.stderr)

    def test_post_package_verification_requires_firewall_offline_cmd(self):
        result = bash(r'''DISTRO_FAMILY=RHEL
have_command() { [[ "$1" != firewall-offline-cmd ]]; }
post_package_verify''')
        self.assertEqual(result.returncode, 1)
        self.assertIn("firewall-offline-cmd", result.stderr)

    def test_preflight_fatal_status_wins_over_later_installable_status(self):
        result = bash(r'''detect_distro() { DISTRO_ID=fedora; DISTRO_VERSION=44; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }
repo_files_ok() { :; }; have_command() { return 0; }; unit_present() { return 0; }; unit_state() { :; }; getent() { return 0; }; python3() { return 0; }
discover_backend_dir() { return 1; }; check_lsm() { return 2; }; firewall-cmd() { printf 'inactive\n'; }; systemctl() { printf 'enabled\n'; }
preflight; rc=$?; printf '%s|%s' "$rc" "$PREFLIGHT_STATUS"''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("1|1"), result.stdout)

    def test_lsm_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            fakebin = Path(directory) / "bin"; fakebin.mkdir()
            self.fake_command(fakebin, "getenforce", 'printf "%s\\n" "$GETENFORCE_RESULT"')
            self.fake_command(fakebin, "aa-status", 'test "$AA_ENABLED" = yes || exit 1\nprintf "2 profiles are in enforce mode.\\n"')
            env = os.environ | {"PATH": f"{fakebin}:{os.environ['PATH']}"}
            for mode, expected in (("Enforcing", 0), ("Permissive", 1), ("Disabled", 1)):
                result = bash('DISTRO_FAMILY=RHEL; check_lsm', env | {"GETENFORCE_RESULT": mode})
                self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(bash('DISTRO_FAMILY=RHEL; have_command() { return 1; }; check_lsm').returncode, 2)
            enabled = bash('DISTRO_FAMILY=SUSE; apparmor_interface_enabled() { return 0; }; check_lsm', env | {"AA_ENABLED": "yes"})
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            disabled = bash('DISTRO_FAMILY=SUSE; apparmor_interface_enabled() { return 0; }; check_lsm', env | {"AA_ENABLED": "no"})
            self.assertEqual(disabled.returncode, 1)
            self.assertEqual(bash('DISTRO_FAMILY=SUSE; have_command() { return 1; }; check_lsm').returncode, 2)

    def test_backend_discovery_sources_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); backend = root / "libexec" / "cups" / "backend"; backend.mkdir(parents=True)
            config = root / "cups-files.conf"; config.write_text(f"# ServerBin /ignored\nServerBin {backend.parent}\n")
            fakebin = root / "bin"; fakebin.mkdir()
            self.fake_command(fakebin, "stat", 'case "$2" in %u) echo "${STAT_OWNER:-0}";; %a) echo "${STAT_MODE:-755}";; esac')
            self.fake_command(fakebin, "cups-config", 'printf "%s\\n" "$CUPS_SERVERBIN"')
            self.fake_command(fakebin, "rpm", 'printf "%s\\n" "$RPM_PATHS"')
            env = os.environ | {"PATH": f"{fakebin}:{os.environ['PATH']}", "CUPS_SERVERBIN": str(backend.parent), "RPM_PATHS": str(backend)}
            direct = bash(f'discover_backend_dir {shlex.quote(str(config))}; printf "%s" "$BACKEND_DIR"', env)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(Path(direct.stdout.splitlines()[-1]), backend)
            missing = root / "missing.conf"
            cups_config = bash(f'discover_backend_dir {shlex.quote(str(missing))}; printf "%s" "$BACKEND_DIR"', env)
            self.assertEqual(cups_config.returncode, 0, cups_config.stderr)
            rpm = bash(f'discover_backend_dir {shlex.quote(str(missing))}; printf "%s" "$BACKEND_DIR"', env | {"CUPS_SERVERBIN": ""})
            self.assertEqual(rpm.returncode, 0, rpm.stderr)
            self.assertEqual(Path(rpm.stdout.splitlines()[-1]), backend)
            other = root / "other" / "cups" / "backend"; other.mkdir(parents=True)
            config.write_text(f"ServerBin relative/path\n")
            self.assertEqual(bash(f'discover_backend_dir {shlex.quote(str(config))}', env).returncode, 1)
            config.write_text(f"ServerBin {backend.parent}\nServerBin {other.parent}\n")
            self.assertEqual(bash(f'discover_backend_dir {shlex.quote(str(config))}', env).returncode, 1)
            config.write_text(f"ServerBin {backend.parent}\n")
            self.assertEqual(bash(f'discover_backend_dir {shlex.quote(str(config))}', env | {"STAT_MODE": "775"}).returncode, 1)
            self.assertEqual(bash(f'discover_backend_dir {shlex.quote(str(config))}', env | {"STAT_OWNER": "1000"}).returncode, 1)
            target = root / "backend-target"
            target.write_text("not a backend\n")
            (backend / "sapfax").symlink_to(target)
            self.assertEqual(bash(f'discover_backend_dir {shlex.quote(str(config))}', env).returncode, 1)
            (backend / "sapfax").unlink()
            self.assertEqual(bash(f'discover_backend_dir {shlex.quote(str(missing))}', env | {"CUPS_SERVERBIN": "", "RPM_PATHS": ""}).returncode, 2)

    def test_env_preservation_and_ownership_mode_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); schema = root / ".env.example"; target = root / ".env"; schema.write_text("SCHEMA=blank\n")
            original = b"RC_CLIENT_ID=fixture\nRC_JWT_TOKEN=fixture\n"; target.write_bytes(original); log = root / "calls"
            result = bash(f'REPO_DIR={shlex.quote(str(root))}; ENV_FILE={shlex.quote(str(target))}; run_cmd() {{ printf "%s\\n" "$*" >> {shlex.quote(str(log))}; }}; stat() {{ case "$2" in %U:%G) echo root:lp;; %a) echo 640;; esac; }}; ensure_env')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), original)
            self.assertIn("chown root:lp", log.read_text()); self.assertIn("chmod 0640", log.read_text())
            failed = bash(f'ENV_FILE={shlex.quote(str(target))}; run_cmd() {{ :; }}; stat() {{ case "$2" in %U:%G) echo root:lp;; %a) echo 600;; esac; }}; verify_env "$ENV_FILE"')
            self.assertEqual(failed.returncode, 1)
            link = root / "symlink.env"; link.symlink_to(target)
            self.assertEqual(bash(f'REPO_DIR={shlex.quote(str(root))}; ENV_FILE={shlex.quote(str(link))}; ensure_env').returncode, 1)

    def test_selinux_idempotency_and_exact_label_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "calls"
            existing = bash(f'''DISTRO_FAMILY=RHEL; SPOOL_DIR=/fixture/spool; BACKEND_TARGET=/fixture/backend/sapfax
semanage() {{ if [[ "$*" == "fcontext -l" ]]; then printf '%s all files system_u:object_r:print_spool_t:s0\\n' "$SPOOL_DIR(/.*)?"; fi; }}
run_cmd() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; }}; verify_selinux_label() {{ [[ "$2" == print_spool_t ]]; }}; verify_selinux_default_label() {{ :; }}; configure_lsm''')
            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertIn("semanage fcontext -m -t print_spool_t /fixture/spool(/.*)?", log.read_text())
            log.unlink()
            absent = bash(f'''DISTRO_FAMILY=RHEL; SPOOL_DIR=/fixture/spool; BACKEND_TARGET=/fixture/backend/sapfax
semanage() {{ [[ "$*" == "fcontext -l" ]] && printf '%s\\n' '/other(/.*)? all files'; }}
run_cmd() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; }}; verify_selinux_label() {{ :; }}; verify_selinux_default_label() {{ :; }}; configure_lsm''')
            self.assertEqual(absent.returncode, 0, absent.stderr)
            self.assertIn("semanage fcontext -a -t print_spool_t /fixture/spool(/.*)?", log.read_text())
        self.assertEqual(bash("ls() { printf 'system_u:object_r:wrong_t:s0 /fixture\\n'; }; verify_selinux_label /fixture print_spool_t").returncode, 1)
        self.assertEqual(bash("ls() { printf 'system_u:object_r:one_t:s0 /fixture\\n'; }; matchpathcon() { printf 'system_u:object_r:two_t:s0 /fixture\\n'; }; verify_selinux_default_label /fixture").returncode, 1)

    def test_selinux_default_label_uses_fedora_supported_matchpathcon_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "matchpathcon-argv"
            result = bash(f'''ls() {{ printf 'system_u:object_r:cupsd_exec_t:s0 /fixture/backend/sapfax\\n'; }}
matchpathcon() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; printf 'system_u:object_r:cupsd_exec_t:s0\\n'; }}
verify_selinux_default_label /fixture/backend/sapfax''')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(), "-n /fixture/backend/sapfax\n")

    def test_preflight_and_setup_reject_unsafe_deployment_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "regular"
            regular.write_text("not a directory\n")
            app_target = root / "app-target"
            app_target.mkdir()
            app_link = root / "app-link"
            app_link.symlink_to(app_target, target_is_directory=True)
            spool = root / "spool"
            spool.mkdir()
            common = r'''detect_distro() { DISTRO_ID=fedora; DISTRO_VERSION=44; DISTRO_FAMILY=RHEL; PACKAGE_MANAGER=dnf; PACKAGES=(cups); }
repo_files_ok() { :; }; have_command() { return 0; }; unit_present() { return 0; }; unit_state() { :; }; getent() { return 0; }; python3() { return 0; }
discover_backend_dir() { return 0; }; check_lsm() { return 0; }; firewall-cmd() { printf 'inactive\n'; }; systemctl() { printf 'enabled\n'; }
'''
            for app_dir, spool_dir in ((app_link, spool), (regular, spool), (spool, regular)):
                with self.subTest(app_dir=app_dir, spool_dir=spool_dir):
                    result = bash(common + f'APP_DIR={shlex.quote(str(app_dir))}; SPOOL_DIR={shlex.quote(str(spool_dir))}; ENV_FILE="$APP_DIR/.env"; preflight; rc=$?; printf "%s" "$rc"')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.endswith("1"), result.stdout)
            calls = root / "calls"
            setup = bash(f'APP_DIR={shlex.quote(str(app_link))}; SPOOL_DIR={shlex.quote(str(spool))}; run_cmd() {{ printf "MUTATION\\n" >> {shlex.quote(str(calls))}; }}; create_dirs')
            self.assertEqual(setup.returncode, 1)
            self.assertFalse(calls.exists())
            app = root / "app"
            app.mkdir()
            venv_target = root / "venv-target"
            venv_target.mkdir()
            (app / "venv").symlink_to(venv_target, target_is_directory=True)
            venv = bash(f'APP_DIR={shlex.quote(str(app))}; run_cmd() {{ printf "MUTATION\\n" >> {shlex.quote(str(calls))}; }}; create_venv')
            self.assertEqual(venv.returncode, 1)
            self.assertFalse(calls.exists())

    def test_venv_reuse_requires_safe_tree_and_isolated_python_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fakebin = root / "bin"
            fakebin.mkdir()
            trusted_python = self.fake_command(fakebin, "trusted-python", 'exit 0')
            (fakebin / "python3").symlink_to(trusted_python)
            app = root / "app"
            venv = app / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("home = fixture\n")
            (venv / "bin" / "python").symlink_to(trusted_python)
            log = root / "calls"
            env = os.environ | {"PATH": f"{fakebin}:{os.environ['PATH']}"}
            safe = bash(f'''APP_DIR={shlex.quote(str(app))}
stat() {{ case "$2" in %u) echo 0;; %a) echo 755;; esac; }}
run_cmd() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; }}
create_venv''', env)
            self.assertEqual(safe.returncode, 0, safe.stderr)
            calls = log.read_text()
            self.assertIn(f"{venv / 'bin' / 'python'} -I -m pip --isolated install -r {app}/requirements.txt", calls)
            self.assertNotIn("/bin/pip", calls)
            unsafe_mode = bash(f'''APP_DIR={shlex.quote(str(app))}
stat() {{ case "$2" in %u) echo 0;; %a) [[ "$4" == *pyvenv.cfg ]] && echo 775 || echo 755;; esac; }}
verify_venv_tree''', env)
            self.assertEqual(unsafe_mode.returncode, 1)
            escape = root / "escape-python"
            escape.write_text("not python\n")
            (venv / "bin" / "python").unlink()
            (venv / "bin" / "python").symlink_to(escape)
            unsafe_link = bash(f'''APP_DIR={shlex.quote(str(app))}
stat() {{ case "$2" in %u) echo 0;; %a) echo 755;; esac; }}
verify_venv_tree''', env)
            self.assertEqual(unsafe_link.returncode, 1)

    def test_real_venv_interpreter_uses_venv_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            result = subprocess.run(
                [str(venv / "bin" / "python"), "-I", "-c", "import sys; print(sys.prefix != sys.base_prefix)"],
                check=True, text=True, stdout=subprocess.PIPE,
            )
            self.assertEqual(result.stdout.strip(), "True")

    def test_backend_install_rejects_nonregular_targets_and_bad_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_dir = root / "target-directory"
            target_dir.mkdir()
            rejected = bash(f'BACKEND_TARGET={shlex.quote(str(target_dir))}; run_cmd() {{ printf mutation; }}; install_backend')
            self.assertEqual(rejected.returncode, 1)
            target = root / "sapfax"
            target.write_text("backend\n")
            safe = bash(f'''BACKEND_TARGET={shlex.quote(str(target))}
run_cmd() {{ :; }}
stat() {{ case "$2" in %u) echo 0;; %a) echo 755;; esac; }}
install_backend''')
            self.assertEqual(safe.returncode, 0, safe.stderr)
            metadata_failure = bash(f'''BACKEND_TARGET={shlex.quote(str(target))}
run_cmd() {{ :; }}
stat() {{ case "$2" in %u) echo 0;; %a) echo 700;; esac; }}
install_backend''')
            self.assertEqual(metadata_failure.returncode, 1)

    def test_queue_and_firewall_idempotent_paths_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "calls"
            queue = bash(f'QUEUE=sap_rfax; run_cmd() {{ printf "%s\\n" "$*" >> {shlex.quote(str(log))}; }}; lpstat() {{ printf "device for sap_rfax: sapfax:/\\n"; }}; configure_queue; configure_queue')
            self.assertEqual(queue.returncode, 0, queue.stderr)
            self.assertEqual(log.read_text().count("lpadmin -p sap_rfax -E -v sapfax:/ -m raw"), 2)
            log.unlink()
            common = f'''FIREWALL_ZONE=public; CIDR_FAMILY=4; CIDR_CANONICAL=192.0.2.0/24
known_zones() {{ printf 'public trusted\\n'; }}; run_cmd() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; }}
'''
            unknown_zone = bash(f'''FIREWALL_ZONE=unknown; CIDR_FAMILY=4; CIDR_CANONICAL=192.0.2.0/24
known_zones() {{ printf 'public trusted\\n'; }}; run_cmd() {{ printf 'MUTATION\\n' >> {shlex.quote(str(log))}; }}
configure_firewall''')
            self.assertEqual(unknown_zone.returncode, 1)
            self.assertFalse(log.exists())
            existing = bash(common + r'''
firewall-offline-cmd() { case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) return 0;; esac; }
firewall-cmd() { case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) return 0;; esac; }
configure_firewall''')
            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertNotIn("--add-rich-rule", log.read_text()); self.assertNotIn("firewall-cmd --reload", log.read_text())
            log.unlink()
            inactive_existing = bash(common + r'''
runtime_rich=0
firewall-offline-cmd() { case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) return 0;; esac; }
firewall-cmd() { case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) runtime_rich=$((runtime_rich + 1)); (( runtime_rich == 1 )) && return 1 || return 0;; esac; }
configure_firewall''')
            self.assertEqual(inactive_existing.returncode, 0, inactive_existing.stderr)
            calls = log.read_text(); self.assertNotIn("--add-rich-rule", calls); self.assertIn("firewall-cmd --reload", calls)
            log.unlink()
            absent = bash(common + r'''
offline_rich=0; runtime_rich=0
firewall-offline-cmd() { case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) offline_rich=$((offline_rich + 1)); (( offline_rich == 1 )) && return 1 || return 0;; esac; }
firewall-cmd() { case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) runtime_rich=$((runtime_rich + 1)); (( runtime_rich == 1 )) && return 1 || return 0;; esac; }
configure_firewall''')
            self.assertEqual(absent.returncode, 0, absent.stderr)
            calls = log.read_text(); self.assertIn('--add-rich-rule rule family="ipv4" source address="192.0.2.0/24" port port="515" protocol="tcp" accept', calls); self.assertIn("firewall-cmd --reload", calls)
            log.unlink()
            offline_error = bash(common + r'''
firewall-offline-cmd() { case "$*" in *--query-port=515/tcp*) return 2;; *) return 1;; esac; }
configure_firewall''')
            self.assertEqual(offline_error.returncode, 1)
            self.assertIn("permanent firewall query failed", offline_error.stderr)
            self.assertFalse(log.exists())
            broad = bash(common + r'''
firewall-offline-cmd() { case "$*" in *--query-port=515/tcp*) return 0;; *) return 1;; esac; }
configure_firewall''')
            self.assertEqual(broad.returncode, 1)
            self.assertFalse(log.exists())
            no_rule = bash(f'FIREWALL_ZONE=; ALLOW_CIDR=; run_cmd() {{ printf mutation >> {shlex.quote(str(log))}; }}; configure_firewall')
            self.assertEqual(no_rule.returncode, 0, no_rule.stderr)
            self.assertIn("leaves cups-lpd.socket disabled", no_rule.stdout)
            self.assertFalse(log.exists())

    def test_lpd_socket_staging_managed_activation_and_external_control_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "calls"
            common = f'''FIREWALL_ZONE=public; CIDR_FAMILY=4; CIDR_CANONICAL=192.0.2.0/24
known_zones() {{ printf 'public\\n'; }}
run_cmd() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; }}
firewall-offline-cmd() {{ case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) return 0;; esac; }}
firewall-cmd() {{ case "$*" in *--query-port*|*--query-service*) return 1;; *--query-rich-rule*) return 0;; esac; }}
'''
            managed = bash(common + "stage_lpd_socket; configure_firewall; configure_lpd_socket")
            self.assertEqual(managed.returncode, 0, managed.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(calls[:2], ["systemctl disable cups-lpd.socket", "systemctl stop cups-lpd.socket"])
            self.assertIn("systemctl enable --now firewalld", calls)
            self.assertIn("systemctl enable --now cups-lpd.socket", calls)
            self.assertLess(calls.index("systemctl stop cups-lpd.socket"), calls.index("systemctl enable --now firewalld"))
            self.assertLess(calls.index("systemctl enable --now firewalld"), calls.index("systemctl enable --now cups-lpd.socket"))

            log.unlink()
            external = bash(f'''FIREWALL_ZONE=; LPD_ACTIVATION_ALLOWED=1
run_cmd() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; }}
stage_lpd_socket; configure_firewall; configure_lpd_socket''')
            self.assertEqual(external.returncode, 0, external.stderr)
            self.assertIn("STAGED INSTALLATION", external.stdout)
            self.assertEqual(log.read_text().splitlines(), ["systemctl disable cups-lpd.socket", "systemctl stop cups-lpd.socket"])

    def test_final_verify_requires_active_lpd_only_for_verified_managed_firewall(self):
        common = r'''verify_env() { :; }
verify_backend_target() { :; }
lpstat() { :; }
'''
        managed = bash(common + r'''LPD_ACTIVATION_ALLOWED=1
systemctl() { [[ "$*" == "is-active --quiet cups" || "$*" == "is-active --quiet cups-lpd.socket" ]]; }
final_verify''')
        self.assertEqual(managed.returncode, 0, managed.stderr)

        external_inactive = bash(common + r'''LPD_ACTIVATION_ALLOWED=0
systemctl() { [[ "$*" == "is-active --quiet cups" ]] && return 0; return 3; }
final_verify''')
        self.assertEqual(external_inactive.returncode, 0, external_inactive.stderr)
        self.assertIn("cups-lpd.socket is inactive", external_inactive.stdout)

        external_active = bash(common + r'''LPD_ACTIVATION_ALLOWED=0
systemctl() { return 0; }
final_verify''')
        self.assertEqual(external_active.returncode, 1)
        self.assertIn("must remain inactive", external_active.stderr)

    def test_nonroot_known_zones_uses_running_or_readable_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            zones = Path(directory) / "zones"
            zones.mkdir()
            (zones / "fixture.xml").write_text("<zone/>\n")
            result = bash(f'''is_root() {{ return 1; }}
firewall-cmd() {{ case "$*" in --state) return 1;; esac; }}
firewall_zone_directories() {{ printf '%s\\n' {shlex.quote(str(zones))}; }}
FIREWALL_ZONE=fixture; validate_zone_membership''')
            self.assertEqual(result.returncode, 0, result.stderr)
            unavailable = bash(r'''is_root() { return 1; }
firewall-cmd() { return 1; }
readable_installed_zones() { return 1; }
FIREWALL_ZONE=fixture; validate_zone_membership''')
            self.assertEqual(unavailable.returncode, 1)
            self.assertIn("cannot be verified", unavailable.stderr)

    def test_firewall_query_wrappers_preserve_rich_rule_as_one_argv_element(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offline_log = root / "offline"
            runtime_log = root / "runtime"
            rule = 'rule family="ipv4" source address="192.0.2.0/24" port port="515" protocol="tcp" accept'
            result = bash(f'''FIREWALL_ZONE=public
firewall-offline-cmd() {{ printf '%s\\n' "$#" "$1" "$2" "$3" > {shlex.quote(str(offline_log))}; return 0; }}
firewall-cmd() {{ printf '%s\\n' "$#" "$1" "$2" "$3" "$4" > {shlex.quote(str(runtime_log))}; return 0; }}
firewall_offline_query --query-rich-rule {shlex.quote(rule)}
firewall_runtime_query --query-rich-rule {shlex.quote(rule)}''')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(offline_log.read_text(), f"3\n--zone=public\n--query-rich-rule\n{rule}\n")
            self.assertEqual(runtime_log.read_text(), f"4\n--zone\npublic\n--query-rich-rule\n{rule}\n")

    def test_source_is_non_mutating_and_contains_no_dangerous_operations(self):
        result = subprocess.run(["bash", "-c", f"source {shlex.quote(str(INSTALLER))}; printf ready"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(result.stdout, "ready")
        source = INSTALLER.read_text()
        for forbidden in ("--add-port=515/tcp", "setenforce", "aa-disable", "eval "):
            self.assertNotIn(forbidden, source)
        self.assertIn('if [[ "${BASH_SOURCE[0]}" == "$0" ]]', source)
        self.assertIn("raw-queue capability is required", source)
        self.assertLess(source.index("configure_firewall; configure_lpd_socket"), source.rindex("final_verify"))


if __name__ == "__main__":
    unittest.main()
