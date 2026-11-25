"""Run the instrumented agent locally through the same entrypoint Agent Engine uses.

Default: dry run. The plugin builds the OTLP requests and they are written to out/ instead of
being sent. With --send they go to Agent 365 using the blueprint secret stored in Secret Manager.

    python run_local.py
    python run_local.py "Assess renewal risk for Fabrikam" --send
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from gcp_auth import patch_google_auth_default

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text("utf-8-sig"))
OUT = HERE / "out"


def configure_env(send: bool) -> None:
    os.environ.update({
        "GOOGLE_CLOUD_PROJECT": CONFIG["gcpProject"],
        "GOOGLE_CLOUD_LOCATION": CONFIG["gcpRegion"],
        **CONFIG.get("agentEnv", {}),
        "A365_TENANT_ID": CONFIG["tenantId"],
        "A365_AGENT_APP_ID": CONFIG["agentAppId"],
        "A365_BLUEPRINT_ID": CONFIG.get("agentBlueprintId", ""),
        "A365_AGENT_NAME": CONFIG["agentName"],
        "A365_AGENT_DESCRIPTION": CONFIG.get("agentDescription", ""),
        "A365_CHANNEL": CONFIG.get("channel", "web"),
        "A365_SERVICE_NAME": CONFIG["displayName"],
        "A365_SECRET_NAME": CONFIG["secretName"],
        "A365_SECRET_PROJECT": CONFIG["gcpProject"],
    })
    if send:
        os.environ.pop("A365_DRY_RUN", None)
    else:
        os.environ["A365_DRY_RUN"] = "1"


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


async def run(app, user_id: str, message: str) -> None:
    session = await app.async_create_session(user_id=user_id)
    session_id = _get(session, "id")
    print(f"session {session_id}  user {user_id}")
    async for event in app.async_stream_query(user_id=user_id, session_id=session_id, message=message):
        for part in (_get(_get(event, "content") or {}, "parts") or []):
            call = _get(part, "function_call")
            if call:
                print(f"  -> tool {_get(call, 'name')}")
            text = _get(part, "text")
            if text and _get(event, "author") != "user":
                print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("message", nargs="?", default="Assess renewal risk for Contoso")
    parser.add_argument("--user-id", default=CONFIG.get("testUserId", ""),
                        help="Entra objectId reported as user.id (default: testUserId in config.json)")
    parser.add_argument("--send", action="store_true", help="export the spans to Agent 365")
    args = parser.parse_args()

    configure_env(args.send)
    patch_google_auth_default(CONFIG["gcpProject"])
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.path.insert(0, str(HERE / "agent"))
    import a365_entrypoint

    if not args.send:
        OUT.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        counter = iter(range(1, 100))

        def to_file(body: bytes, names: dict) -> None:
            path = OUT / f"otlp-{stamp}-{next(counter)}.json"
            path.write_bytes(body)
            print(f"[dry run] {len(names)} spans -> {path.name}: {', '.join(sorted(names.values()))}")

        a365_entrypoint.plugin.sender = to_file

    asyncio.run(run(a365_entrypoint.app, args.user_id, args.message))


if __name__ == "__main__":
    main()
