# Privacy and security boundaries

The public project separates portable application code and sanitized examples from private research infrastructure. The measures below describe the publication process and current application boundaries.

## Material excluded or replaced

| Category | Treatment |
|---|---|
| Private absolute paths | Replace with `/opt/kronos`, `/opt/quantweave` or configurable placeholders |
| Usernames and hosts | Remove private usernames, SSH aliases and compute-host names |
| Private network configuration | Exclude private addresses, port forwards and autossh settings |
| Interpreter paths | Use `sys.executable` instead of private conda environment paths |
| SSH and deployment files | Exclude SSH configuration, launch services and personal deployment records |
| Actual accounts and positions | Use synthetic paper-account examples |
| Provider credentials | Exclude tokens, cookies, account credentials and database connection strings |

`KRONOS_ROOT` can override the portable model-root placeholder. Documentation uses repository-relative paths and user-supplied configuration rather than a private workstation layout.

## Application controls

- **Job allowlist:** `POST /api/jobs` accepts enumerated kinds, not arbitrary commands.
- **Argument arrays:** subprocesses use argv with `shell=False`.
- **Parameter validation:** dates, codes, enums and numeric ranges are checked before execution.
- **Download scope:** artifact paths are normalized, traversal is rejected and downloads stay within run/demo artifact roots.
- **Explicit computation:** read-only snapshots do not initiate background calculations.
- **Local binding:** there is no built-in authentication; bind to `127.0.0.1`. Network deployments require authentication and an appropriate reverse proxy.
- **Paper account only:** account operations use local files or demo memory; no brokerage order API is connected.

## Snapshot sanitization

During snapshot preparation, recursively inspect string values for private paths, identities, SSH aliases, hostnames, network addresses and runtime details. Remove them or replace them with portable values. Scan the staged publication content for known private identifiers, then manually review matches and representative payloads.

Pattern searches complement manual inspection; they are not proof that every sensitive value has been removed. Demo account balances, positions and trades are synthetic. Public stock names/codes in examples do not represent a person's holdings.

## Data and operational limits

Frozen index and industry snapshots contain historical market observations alongside synthetic demo series. Public availability does not establish unrestricted redistribution rights; verify the relevant provider's terms before distributing another dataset. This repository does not include model weights, market databases or account credentials.

A new data integration needs its own access controls, authorization review and audit trail. Publication checks combine pattern matching and human review and may miss unexpected formats. Report suspected residual private information through a private channel.

## Reporting a problem

Use GitHub's private vulnerability reporting if it is enabled. Otherwise ask the maintainer for a private contact channel without posting sensitive details. **Repository issues are public** and should not contain credentials, private paths or exploit details that require confidential handling.
