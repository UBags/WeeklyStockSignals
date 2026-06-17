"""Brevo secrets file loader.

A single-purpose secrets file lives outside the config YAML so the
config can stay committable. The file is referenced by path from
`transports.brevo.secrets_file` in config.yaml.

File format (YAML):

    api_key: xkeysib-AAA....ZZZ

Validation at load time (fail-fast):
  - File exists at the configured path.
  - File mode is not world/group-readable. Brevo keys grant full
    send privileges on the account; a 0644 secrets file is a
    real risk on a shared machine, and 0600 is the convention.
  - `api_key` field is present, non-empty, and starts with the
    `xkeysib-` prefix that every Brevo SMTP/API key uses. Catches
    paste errors immediately instead of waiting for a 401 from
    Brevo at first send.

The load happens at startup (called from `load_config`), not at
send time. Matches the existing pattern of `password_env`
resolution: a misconfigured secret should crash the process at
boot, not silently substitute NullTransport.
"""

from __future__ import annotations

import stat
from pathlib import Path

import yaml


class BrevoSecretsError(Exception):
    """Raised when the Brevo secrets file is missing, malformed, or
    insecurely permissioned. Wrapped by config.loader.ConfigError so
    callers see a uniform startup-failure type."""


_BREVO_KEY_PREFIX = "xkeysib-"


def load_brevo_api_key(path: str | Path) -> str:
    """Read, validate, and return the Brevo API key from `path`.

    Raises:
        BrevoSecretsError: file missing, world/group-readable,
            malformed YAML, missing/empty/malformed `api_key` field.

    Returns:
        The api_key string with surrounding whitespace stripped.
    """
    p = Path(path)
    if not p.exists():
        raise BrevoSecretsError(f"Brevo secrets file not found: {p}")
    if not p.is_file():
        raise BrevoSecretsError(f"Brevo secrets path is not a regular file: {p}")

    # Permission check. The mode bits we care about are group + other
    # read/write. We don't enforce owner-execute or umask-quirky bits.
    # The check is intentionally permissive about owner perms (could
    # be 0600 or 0400 or 0700) but strict about group/world readability.
    file_mode = p.stat().st_mode
    forbidden = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
    if file_mode & forbidden:
        raise BrevoSecretsError(
            f"Brevo secrets file has insecure permissions: {p} "
            f"(mode={oct(stat.S_IMODE(file_mode))}). "
            f"Fix with: chmod 600 {p}"
        )

    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise BrevoSecretsError(f"YAML parse error in {p}: {e}") from e

    if not isinstance(data, dict):
        raise BrevoSecretsError(
            f"Brevo secrets file must contain a YAML mapping at top level: {p}"
        )

    raw_key = data.get("api_key")
    if not isinstance(raw_key, str):
        raise BrevoSecretsError(
            f"Brevo secrets file missing required field 'api_key' (string): {p}"
        )
    key = raw_key.strip()
    if not key:
        raise BrevoSecretsError(f"Brevo secrets file 'api_key' is empty: {p}")
    if not key.startswith(_BREVO_KEY_PREFIX):
        # Don't echo the (partial) key into the error message — it ends up
        # in logs. Just describe the shape we expected.
        raise BrevoSecretsError(
            f"Brevo secrets file 'api_key' has unexpected format "
            f"(expected prefix '{_BREVO_KEY_PREFIX}'): {p}"
        )

    return key
