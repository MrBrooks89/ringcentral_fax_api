# Agent Instructions

## Scope and context

- Work from the repository root: the root directory of this checkout.
- Read this file and `README.md` before changing the project.
- This repository implements a Linux CUPS/LPD gateway that converts SAP print jobs to PDF and sends them through the RingCentral Fax API.
- Keep changes compatible with the exact supported IDs: `rhel`, `fedora`,
  `centos`, `rocky`, `almalinux`, `opensuse-leap`, `opensuse-tumbleweed`, and
  `sles`. Match `/etc/os-release` `ID` exactly; do not infer support from
  `ID_LIKE`.
- The installer is cross-distribution but fail-closed: RHEL-family hosts use
  dnf/SELinux enforcing, and SUSE-family hosts use zypper/AppArmor enabled.

## Build and dependencies

- Use Python 3 and a repository-local virtual environment (`.venv`); never install project packages globally.
- Create the environment with `python3 -m venv .venv` and install dependencies with `.venv/bin/python -m pip install -r requirements.txt` when dependency installation is needed.
- Do not run `install.sh` during ordinary development or validation. It requires root and mutates CUPS, firewalld, SELinux, `/opt/ringcentral-fax`, `/var/spool/ringcentral-fax`, and system services.
- Treat `requirements.txt`, the Python files, the CUPS backend, and installer changes as one deployment surface; review their compatibility together when any of them changes.

## Testing and validation

- Run the smallest relevant checks first. The current baseline checks are:
  - `python3 -m py_compile process_print_job.py send_fax.py`
  - `bash -n install.sh`
- `python3 -m unittest discover -s tests -v`
- If tests are added, run them with `.venv/bin/python -m pytest -q` (or `python3 -m pytest -q` when the virtual environment is not needed) and keep them hermetic.
- Mock RingCentral SDK authentication, API requests, subprocess calls, time, and production filesystem paths in automated tests.
- Installer tests must mock every mutating command and use temporary fixtures;
  never exercise package, service, CUPS, firewalld, SELinux, or AppArmor
  mutation on the host.
- Installer functions must be source-safe; only `install.sh --check` is safe
  for ordinary host inspection. Internal unit tests may source functions with
  temporary fixtures and fake commands, but production `main` must provide no
  root bypass or production-path override option.
- Use temporary directories for spool/output tests. Do not write test artifacts under `/var/spool/ringcentral-fax` or `/opt/ringcentral-fax`.
- Do not send a real fax, authenticate to RingCentral, expose TCP/515, change CUPS queues, modify SELinux/firewall rules, start or stop services, or run privileged installation steps unless the user explicitly requests that exact integration test.
- Review both SELinux and AppArmor behavior; never disable an LSM, use broad
  TCP/515 firewall rules, or commit site-specific installer values/CIDRs.
- When production-only behavior cannot be exercised safely, report the untested boundary and the exact manual validation still required.

## Git and collaboration

- Preserve user changes and inspect `git status` before editing.
- Keep patches focused; do not reformat or rewrite unrelated files.
- Role agents share this worktree. Do not discard, overwrite, stash, or revert another agent's work.
- Integration, implementation, validation, security, and release agents must not commit, push, open a PR, merge, or change branches unless the coordinator explicitly instructs them to do so.
- The coordinator owns the final commit and PR. Before committing, review the complete diff and validation/security results, then use a concise imperative commit message.
- Never force-push or rewrite published history without explicit user approval.

## Security and production safety

- Never commit `.env`, tokens, client secrets, JWTs, phone numbers, customer documents, fax payloads, message IDs, or unsanitized spool files.
- Use obviously fake values in examples and tests. Redact secrets and personal data from logs, terminal output, review notes, commits, and PR text.
- Keep `.env` ignored and preserve least-privilege ownership/modes documented in `README.md`.
- Treat incoming print data, filenames, metadata fields, and fax destinations as untrusted input. Review parsing, path handling, subprocess use, resource limits, logging, and error messages accordingly.
- Avoid shell invocation for untrusted values. Prefer argument lists and explicit validation.
- Any change that broadens network exposure, weakens permissions, bypasses TLS verification, logs document contents, or changes authentication requires an explicit security review and clear user approval before deployment.

## Herdr role workflow

For feature work requested through the coordinator:

1. `integration` reviews the proposed design and identifies cross-file or deployment concerns.
2. `implementation` makes the approved changes.
3. `validation` runs relevant hermetic checks and reports evidence and gaps.
4. `security` reviews risks relevant to the change.
5. `integration` performs a final diff review.
6. `release` checks documentation, compatibility, validation evidence, Git state, and PR readiness.
7. The coordinator performs the final review, commits, pushes, and opens the PR when authorized.

Agents should report actionable findings with file/line references, commands run, and any remaining uncertainty. A role that finds a blocker must stop risky follow-on work and notify the coordinator.
