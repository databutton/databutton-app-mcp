import argparse
import pathlib
import base64
import json
import asyncio
import signal
import sys
import os

from websockets import Subprotocol, connect
from websockets.asyncio.client import ClientConnection


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
    # Set up signal handling for graceful exit
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, loop.stop)

    auth_headers: list[tuple[str, str]] = []
    if bearer:
        auth_headers.append(("Authorization", f"Bearer {bearer}"))

    auth_subprotocols: list[Subprotocol] = []
    # auth_subprotocols.append(Subprotocol(f"Authorization.Bearer.{bearer}"))

    async with connect(
        uri,
        subprotocols=[Subprotocol("mcp")] + auth_subprotocols,
        additional_headers=auth_headers,
    ) as websocket:
        stdin_task = asyncio.create_task(stdin_to_ws(websocket))
        stdout_task = asyncio.create_task(ws_to_stdout(websocket))

        try:
            await asyncio.gather(stdin_task, stdout_task)
        except asyncio.CancelledError:
            print("Connection terminated", file=sys.stderr)
        finally:
            stdin_task.cancel()
            stdout_task.cancel()


def parse_apikey(apikey: str) -> dict[str, str]:
    if not apikey:
        raise ValueError("API key must be provided")

    print(apikey)

    try:
        decoded = base64.urlsafe_b64decode(apikey).decode()
        print("json.loads:")
        print(decoded)
        return json.loads(decoded)
    except Exception as e:
        print(f"Failed to parse API key: {e}")
        pass

    try:
        decoded = base64.b64decode(apikey).decode()
        print("json.loads:")
        print(decoded)
        return json.loads(decoded)
    except Exception as e:
        print(f"Failed to parse API key: {e}")
        pass

    try:
        print("json.loads:")
        print(apikey)
        return json.loads(apikey)
    except Exception as e:
        print(f"Failed to parse API key: {e}")
        pass

    raise ValueError("Invalid API key")


DATABUTTON_API_KEY = "DATABUTTON_API_KEY"


def main():
    parser = argparse.ArgumentParser(
        description="Expose Databutton app endpoints as LLM tools with MCP over websocket"
    )
    parser.add_argument(
        "-k",
        "--apikeyfile",
        dest="apikeyfile",
        type=str,
        help="File containing API key to use",
        required=False,
    )
    args = parser.parse_args()

    env_apikey = os.environ.get(DATABUTTON_API_KEY)

    if not (args.apikeyfile or env_apikey):
        print("No API key provided")
        sys.exit(1)

    if args.apikeyfile and pathlib.Path(args.apikeyfile).exists():
        apikey = pathlib.Path(args.apikeyfile).read_text()
    else:
        apikey = env_apikey

    if not apikey:
        print("Provided API key is blank")
        sys.exit(1)

    claims: dict[str, str] = {}
    try:
        claims = parse_apikey(apikey)
    except Exception as e:
        print(f"Failed to parse API key: {e}")
        sys.exit(1)

    uri = claims.get("uri")
    if not uri:
        print("URI must be provided")
        sys.exit(1)
    if not (
        uri.startswith("ws://localhost")
        or uri.startswith("ws://127.0.0.1:")
        or uri.startswith("wss://")
    ):
        print("URI must start with 'ws://' or 'wss://'")
        sys.exit(1)

    # TODO: Exchange refresh token for access token here
    accessToken: str | None = claims.get("accessToken")
    if claims.get("refreshToken"):
        pass

    try:
        asyncio.run(
            run_ws_proxy(
                uri=uri,
                bearer=accessToken,
            )
        )
    except KeyboardInterrupt:
        print("Program terminated", file=sys.stderr)


if __name__ == "__main__":
    main()
