"""
Modular MCP Server for Garmin Connect Data.

Public entry points:
- ``build_app(streamable_http_path)`` -> a fully configured FastMCP app.
- ``main()``                          -> stdio transport (Claude Desktop) by
                                          default; delegates to the HTTP server
                                          when GARMIN_MCP_TRANSPORT is http/
                                          streamable-http.

Diagnostics go to stderr so they never corrupt the stdio JSON-RPC stream.
"""

import json
import os
import sys

import requests
from mcp.server.fastmcp import FastMCP

from garth.exc import GarthHTTPError
from garminconnect import Garmin, GarminConnectAuthenticationError

# Import all tool modules
from garmin_mcp import activity_management
from garmin_mcp import health_wellness
from garmin_mcp import user_profile
from garmin_mcp import devices
from garmin_mcp import gear_management
from garmin_mcp import weight_management
from garmin_mcp import challenges
from garmin_mcp import training
from garmin_mcp import workouts
from garmin_mcp import data_management
from garmin_mcp import womens_health
from garmin_mcp import recommendations

_MODULES = (
    activity_management,
    health_wellness,
    user_profile,
    devices,
    gear_management,
    weight_management,
    challenges,
    training,
    workouts,
    data_management,
    womens_health,
    recommendations,
)


def _log(*args):
    print(*args, file=sys.stderr, flush=True)


def _to_json_str(data):
    """Convert data to a JSON string if it isn't one already."""
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, indent=2, default=str)
    except (TypeError, ValueError):
        return str(data)


def get_mfa() -> str:
    """Get an MFA code non-interactively (for containerised deployments).

    Sources, in order: GARMIN_MFA_CODE env, GARMIN_MFA_CODE_FILE, then poll
    for up to GARMIN_MFA_WAIT_SECONDS for either to appear.
    """
    _log("\nGarmin Connect MFA required. Awaiting code via env or file...")

    mfa_code = os.environ.get("GARMIN_MFA_CODE")
    if mfa_code:
        return mfa_code.strip()

    mfa_file = os.environ.get("GARMIN_MFA_CODE_FILE")
    if mfa_file and os.path.exists(os.path.expanduser(mfa_file)):
        with open(os.path.expanduser(mfa_file), "r") as f:
            return f.read().strip()

    wait_seconds = int(os.environ.get("GARMIN_MFA_WAIT_SECONDS", "0") or 0)
    if wait_seconds > 0:
        import time

        end_time = time.time() + wait_seconds
        while time.time() < end_time:
            mfa_code = os.environ.get("GARMIN_MFA_CODE")
            if mfa_code:
                return mfa_code.strip()
            mfa_file = os.environ.get("GARMIN_MFA_CODE_FILE")
            if mfa_file and os.path.exists(os.path.expanduser(mfa_file)):
                with open(os.path.expanduser(mfa_file), "r") as f:
                    return f.read().strip()
            time.sleep(1)

    raise RuntimeError(
        "MFA code required but not provided. Set GARMIN_MFA_CODE or "
        "GARMIN_MFA_CODE_FILE (optional: GARMIN_MFA_WAIT_SECONDS to poll)."
    )


# Credentials / token sources from environment
email = os.environ.get("GARMIN_EMAIL")
password = os.environ.get("GARMIN_PASSWORD")
TOKENSTORE_DIR = os.path.expanduser(os.getenv("GARMINTOKENS") or "~/.garminconnect")
TOKEN_BASE64 = os.getenv("GARMIN_TOKEN_BASE64") or os.getenv("GARMINTOKENS_BASE64")


def _persist_tokens(garmin):
    """Persist OAuth tokens to the token directory (the Railway volume)."""
    try:
        os.makedirs(TOKENSTORE_DIR, exist_ok=True)
        garmin.garth.dump(TOKENSTORE_DIR)
        _log(f"OAuth tokens persisted to '{TOKENSTORE_DIR}'.")
    except Exception as e:  # noqa: BLE001 - best effort
        _log(f"Warning: could not persist tokens to '{TOKENSTORE_DIR}': {e}")


def init_api(email, password):
    """Initialise the Garmin API client.

    Order of preference (fewest network handshakes first, so a fingerprint-
    sensitive full login is the last resort):
      1. base64 token blob (GARMIN_TOKEN_BASE64) - no login handshake.
      2. cached tokens in the token directory / persistent volume.
      3. email + password (+ MFA) full login.
    """
    # 1) base64 token blob (recommended for cloud deploys)
    if TOKEN_BASE64 and len(TOKEN_BASE64) > 512:
        try:
            _log("Logging in to Garmin using the provided base64 token blob...")
            garmin = Garmin()
            garmin.login(TOKEN_BASE64)  # >512 chars -> garth.loads()
            _persist_tokens(garmin)
            return garmin
        except Exception as e:  # noqa: BLE001
            _log(f"Base64 token login failed, will try other methods: {e}")

    # 2) cached tokens on disk (persistent volume)
    try:
        _log(f"Logging in to Garmin using cached tokens in '{TOKENSTORE_DIR}'...")
        garmin = Garmin()
        garmin.login(TOKENSTORE_DIR)  # short path -> garth.load(dir)
        return garmin
    except (
        FileNotFoundError,
        GarthHTTPError,
        GarminConnectAuthenticationError,
        KeyError,
    ) as e:
        _log(f"No usable cached tokens ({type(e).__name__}: {e}).")

    # 3) full credential login (requires an un-fingerprinted source IP)
    if not email or not password:
        _log(
            "No cached tokens and GARMIN_EMAIL/GARMIN_PASSWORD are not set; "
            "cannot authenticate."
        )
        return None
    try:
        _log("Logging in to Garmin with email/password (may require MFA)...")
        garmin = Garmin(
            email=email, password=password, is_cn=False, prompt_mfa=get_mfa
        )
        garmin.login()
        _persist_tokens(garmin)
        try:
            blob = garmin.garth.dumps()
            _log(
                "\n===== GARMIN_TOKEN_BASE64 (store as a secret to skip future "
                "logins/MFA) =====\n" + blob +
                "\n===== end GARMIN_TOKEN_BASE64 =====\n"
            )
        except Exception:  # noqa: BLE001
            pass
        return garmin
    except (
        FileNotFoundError,
        GarthHTTPError,
        GarminConnectAuthenticationError,
        requests.exceptions.HTTPError,
    ) as err:
        _log(f"Garmin login failed: {err}")
        return None


def _configure_transport_security(app):
    """Configure the MCP Streamable HTTP DNS-rebinding host check.

    The MCP SDK's transport rejects any Host header not in its allow-list with
    421. Behind a proxy (Railway) the public host isn't localhost, so this
    would block every request. Our own bearer-auth middleware already runs
    *before* the MCP app, so an unauthorized caller never reaches this check —
    the host check is redundant here. We therefore disable it by default, but:
      - auto-allow the Railway public domain (RAILWAY_PUBLIC_DOMAIN) and any
        hosts in GARMIN_MCP_ALLOWED_HOSTS, and
      - re-enable the check when GARMIN_MCP_ENABLE_HOST_CHECK is truthy.
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception:  # noqa: BLE001 - older SDKs have no such setting
        return

    enable = os.environ.get("GARMIN_MCP_ENABLE_HOST_CHECK", "").lower() in (
        "1", "true", "yes", "on",
    )

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

    extra = []
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        extra.append(os.environ["RAILWAY_PUBLIC_DOMAIN"].strip())
    extra += [
        h.strip()
        for h in os.environ.get("GARMIN_MCP_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    ]
    for h in extra:
        hosts.extend([h, f"{h}:*"])
        origins.extend([f"https://{h}", f"https://{h}:*"])

    try:
        app.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=enable,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"Warning: could not set transport security settings: {e}")


# Set once build_app() runs; the live dashboard route reads it.
GARMIN_CLIENT = None


def get_client():
    """Return the initialized Garmin client (or None before build_app runs)."""
    return GARMIN_CLIENT


def build_app(streamable_http_path: str = "/mcp") -> FastMCP:
    """Build a fully configured FastMCP app (Garmin client + all tools)."""
    global GARMIN_CLIENT
    garmin_client = init_api(email, password)
    if not garmin_client:
        raise RuntimeError("Failed to initialize Garmin Connect client.")
    GARMIN_CLIENT = garmin_client
    _log("Garmin Connect client initialized successfully.")

    for module in _MODULES:
        module.configure(garmin_client)

    app = FastMCP("Garmin Connect v1.0")
    # Binding/port is controlled by uvicorn in http_server; only the mount path
    # matters here. Default is already "/mcp"; set explicitly for clarity.
    try:
        app.settings.streamable_http_path = streamable_http_path
    except Exception:  # noqa: BLE001 - tolerate older/newer settings shapes
        pass

    _configure_transport_security(app)

    for module in _MODULES:
        app = module.register_tools(app)

    @app.tool()
    async def list_activities(limit: int = 5) -> str:
        """List recent Garmin activities"""
        try:
            activities = garmin_client.get_activities(0, limit)
            if not activities:
                return "No activities found."
            return _to_json_str(activities)
        except Exception as e:  # noqa: BLE001
            return f"Error retrieving activities: {str(e)}"

    return app


def main():
    """Entry point. Defaults to stdio (Claude Desktop); HTTP when requested."""
    transport = os.environ.get("GARMIN_MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http", "sse"):
        # HTTP transports require the bearer-auth wrapper and $PORT handling.
        from garmin_mcp.http_server import serve

        serve()
        return

    app = build_app()
    _log("Garmin MCP server ready (stdio transport).")
    app.run()


if __name__ == "__main__":
    main()
