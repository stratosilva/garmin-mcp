"""Interactively create a native Garmin session and store it as a Railway secret.

This helper intentionally never writes the password, MFA code, or session token
to disk or to standard output.  Run it in a local terminal with ``uv run``.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys

from garminconnect import Garmin


def prompt_mfa() -> str:
    """Prompt locally for a Garmin MFA code without echoing it."""
    return getpass.getpass("Garmin MFA code: ")


def main() -> int:
    print("This sign-in stays in this terminal. Your password and MFA code are hidden.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        return 2

    print("Signing in to Garmin…")
    garmin = Garmin(email=email, password=password, is_cn=False, prompt_mfa=prompt_mfa)
    garmin.login()
    # Native GarminConnect tokens include an auto-refreshing DI refresh token.
    token = garmin.client.dumps().encode("utf-8")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    railway = os.path.join(project_root, "..", ".railway-tools", "node_modules", ".bin", "railway.cmd")
    command = [railway, "variable", "set", "GARMIN_TOKEN_JSON", "--stdin", "--service", "garmin-mcp"]
    print("Saving the refreshed session securely to Railway…")
    result = subprocess.run(command, input=token, cwd=project_root, check=False)
    if result.returncode:
        print("Railway did not accept the session. Nothing was displayed or saved locally.", file=sys.stderr)
        return result.returncode

    print("Done. Railway is restarting the dashboard with the refreshed session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
