import argparse
from typing import Any
import httpx
import asyncio
import base64
import json
import logging
import os
import pathlib
import signal
import ssl
import sys

import certifi
from websockets import Subprotocol, connect
from websockets.asyncio.client import ClientConnection
from websockets import exceptions

logger = logging.getLogger("databutton-app-mcp")


async def stdin_to_ws(websocket: ClientConnection):
    """Read from stdin and send to websocket"""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:  # EOF
            break
        await websocket.send(line.rstrip("\n"))


async def ws_to_stdout(websocket: ClientConnection):
    """Receive from websocket and write to stdout"""
    async for msg in websocket:
        print(msg, flush=True)


async def run_ws_proxy(uri: str, bearer: str | None = None):
    logger.info(f"Connecting to mcp server at {uri}")

    use_ssl = uri.startswith("wss://")
    if not use_ssl:
        logger.warning("Using insecure websocket connection")
    ssl_context = (
        ssl.create_default_context(cafile=certifi.where()) if use_ssl else None
    )

    # add_signal_handler doesn't support Windows
    if sys.platform != "win32":
        # Set up signal handling for graceful exit
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, loop.stop)
        loop.add_signal_handler(signal.SIGTERM, loop.stop)

    subprotocols = [Subprotocol("mcp")]

    auth_headers: list[tuple[str, str]] = []
    if bearer:
        auth_headers.append(("Authorization", f"Bearer {bearer}"))
        # This trick allows sending the Authorization header with the mcp subprotocol from a browser,
        # but we don't need to do that here since we can set the header directly
        # subprotocols.append(Subprotocol(f"Authorization.Bearer.{bearer}"))

    try:
        async with connect(
            uri,
            subprotocols=subprotocols,
            additional_headers=auth_headers,
            open_timeout=60,
            ping_interval=10,
            ping_timeout=10,
            user_agent_header="databutton-app-mcp",  # +f"/{__version__}",
            ssl=ssl_context,
        ) as websocket:
            logger.info("Connection established")

            stdin_task = asyncio.create_task(stdin_to_ws(websocket))
            stdout_task = asyncio.create_task(ws_to_stdout(websocket))

            try:
                await asyncio.gather(stdin_task, stdout_task)
            except asyncio.CancelledError:
                logger.error("Connection terminated")
            finally:
                stdin_task.cancel()
                stdout_task.cancel()
    except exceptions.ConnectionClosedOK:
        logger.error("Connection closed cleanly")
    except exceptions.ConnectionClosedError as e:
        logger.error(f"Connection closed with error: {e}")
    except exceptions.InvalidStatus as e:
        if e.response.status_code == 502:
            if "prodx" in uri:
                logger.error(
                    "Connection refused: has the Databutton app been deployed after enabling MCP?"
                )
            else:
                logger.error(
                    "Connection refused: has MCP been enabled in the Databutton app?"
                )
    except exceptions.InvalidHandshake as e:
        logger.error(f"Connection handshake failed: {e}")
    except exceptions.WebSocketException as e:
        logger.error(f"Unhandled websocket error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")


def safe_base64url_decode(data: str) -> bytes:
    data = data.strip()
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def decode_base64_json(b: str) -> Any:
    return json.loads(safe_base64url_decode(b))


def parse_apikey(apikey: str) -> dict[str, str]:
    """Legacy API key format from initial testing."""
    if not apikey:
        raise ValueError("API key must be provided")

    try:
        decoded = base64.urlsafe_b64decode(apikey).decode()
        return json.loads(decoded)
    except Exception:
        pass

    try:
        decoded = base64.b64decode(apikey).decode()
        return json.loads(decoded)
    except Exception:
        pass

    try:
        return json.loads(apikey)
    except Exception:
        pass

    raise ValueError("Invalid API key")


def interpret_apikey(apikey: str) -> tuple[str, str | None]:
    """Parse the Databutton app api key and return the wss uri and access token."""
    prefix = "dbtk-v1-"
    if apikey.startswith(prefix):
        apikey_contents = decode_base64_json(apikey.replace(prefix, ""))
        bearer = get_access_token(apikey_contents.get("tok"))
        bearer_claims = decode_base64_json(bearer.split(".")[1])
        dbtn_claims = bearer_claims.get("dbtn")
        appId = dbtn_claims.get("appId")
        env = dbtn_claims.get("env")
        uri = f"wss://api.databutton.com/_projects/{appId}/dbtn/{env}/app/mcp/ws"
        return uri, bearer
    else:
        # Legacy API key format from initial testing
        dbtn_claims: dict[str, str] = {}
        dbtn_claims = parse_apikey(apikey)
        uri = dbtn_claims.get("uri")
        if not uri:
            raise ValueError("Missing URI in api key")
        if not (
            uri.startswith("ws://localhost")
            or uri.startswith("ws://127.0.0.1:")
            or uri.startswith("wss://")
        ):
            raise ValueError("URI must start with 'ws://' or 'wss://'")
        return uri, dbtn_claims.get("authCode")


def get_access_token(refresh_token: str) -> str:
    """Get firebase access token from a refresh token."""
    public_firebase_api_key = "AIzaSyAdgR9BGfQrV2fzndXZLZYgiRtpydlq8ug"
    response = httpx.post(
        f"https://securetoken.googleapis.com/v1/token?key={public_firebase_api_key}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    return response.json().get("id_token")


DATABUTTON_API_KEY = "DATABUTTON_API_KEY"

description = "Expose Databutton app endpoints as LLM tools with MCP over websocket"

epilog = f"""\
Instead of providing an API key filepath with -k, you can set the {DATABUTTON_API_KEY} environment variable.

Go to https://databutton.com to build apps and get your API key.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        prog="databutton-app-mcp",
        usage="uvx databutton-app-mcp@latest [-h] [-k APIKEYFILE] [-v]",
        description="Expose Databutton app endpoints as LLM tools with MCP over websocket",
        epilog=epilog,
    )
    parser.add_argument(
        "-k",
        "--apikeyfile",
        help="File containing the API key",
        required=False,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Run in verbose mode with info logging",
        action="store_true",
    )
    parser.add_argument(
        "-d",
        "--debug",
        help="Run in very verbose mode with debug logging",
        action="store_true",
    )
    parser.add_argument(
        "--show-uri",
        help="Show uri it would connect to and exit",
        action="store_true",
    )
    parser.add_argument(
        "-u",
        "--uri",
        help="Use a custom uri for the MCP server endpoint",
        default="",
    )
    return parser.parse_args()


def main():
    try:
        args = parse_args()
        env_apikey = os.environ.get(DATABUTTON_API_KEY)
    except Exception as e:
        logger.error(f"Error while parsing input: {e}")
        sys.exit(1)

    level = (
        logging.DEBUG
        if args.debug
        else (logging.INFO if args.verbose else logging.WARNING)
    )
    logging.basicConfig(
        level=level,
        format="databutton-app-mcp %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        logger.info("Starting Databutton app MCP proxy")
        if not (args.apikeyfile or env_apikey):
            logger.error("No API key provided")
            sys.exit(1)

        if args.apikeyfile and pathlib.Path(args.apikeyfile).exists():
            logger.info(f"Using api key from file {args.apikeyfile}")
            apikey = pathlib.Path(args.apikeyfile).read_text().strip()
        else:
            logger.info("Using api key from environment variable")
            apikey = env_apikey

        if not apikey:
            logger.error("Provided API key is blank")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to get API key: {e}")
        sys.exit(1)

    try:
        uri, bearer = interpret_apikey(apikey)
    except Exception as e:
        logger.error(f"Failed to interpret API key: {e}")
        sys.exit(1)

    if args.uri:
        logger.info(f"Using override uri from command line: {args.uri}")
        uri = args.uri

    if args.show_uri:
        print("databutton-app-mcp would connect to:")
        print(uri)
        sys.exit(0)

    try:
        asyncio.run(
            run_ws_proxy(
                uri=uri,
                bearer=bearer,
            )
        )
    except KeyboardInterrupt:
        logger.error("Program terminated")
        sys.exit(1)


if __name__ == "__main__":
    main()
