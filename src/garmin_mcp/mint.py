"""Mint Garmin OAuth tokens locally and print a base64 blob for Railway.

Run this on YOUR OWN machine (not a cloud IP). It performs the full Garmin
login once — interactively prompting for an MFA code if required — then prints
a single-line base64 token blob to stdout. Set that blob on Railway as
``GARMIN_TOKEN_BASE64`` so the deployed server never performs a login from
Railway's IP range (which Garmin's Cloudflare layer may block).

Usage:
    uv run garmin-mcp-mint            # prompts for email/password/MFA
    # or provide env first:
    GARMIN_EMAIL=you@example.com uv run garmin-mcp-mint > token.b64
"""

import getpass
import os
import sys

from garminconnect import Garmin


def main():
    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass(
        "Garmin password: "
    )

    def prompt_mfa():
        return input("Garmin MFA code (from email/SMS): ").strip()

    garmin = Garmin(email=email, password=password, is_cn=False, prompt_mfa=prompt_mfa)
    garmin.login()

    tokendir = os.path.expanduser(os.getenv("GARMINTOKENS") or "~/.garminconnect")
    os.makedirs(tokendir, exist_ok=True)
    garmin.garth.dump(tokendir)

    blob = garmin.garth.dumps()

    sys.stderr.write(
        f"\nLogged in as {garmin.full_name}. Tokens cached to {tokendir}\n"
        "The single base64 line printed to stdout below is your "
        "GARMIN_TOKEN_BASE64 value.\n"
        "Set it on Railway (railway variables --set ...) and keep it secret.\n\n"
    )
    print(blob)


if __name__ == "__main__":
    main()
