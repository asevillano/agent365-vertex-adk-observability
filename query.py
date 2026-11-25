"""Ask the deployed agent a question (Agent Engine, async_stream_query).

    python query.py "Assess renewal risk for Contoso"
    python query.py "Assess renewal risk for Fabrikam" --user-id <entra-user-objectId>
"""
import argparse
import asyncio
import json
from pathlib import Path

import vertexai

from gcp_auth import gcp_credentials

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text("utf-8-sig"))
STATE = json.loads((HERE / "deployment.json").read_text("utf-8"))


async def run(message: str, user_id: str) -> None:
    client = vertexai.Client(project=CONFIG["gcpProject"], location=CONFIG["gcpRegion"],
                             credentials=gcp_credentials())
    agent = client.agent_engines.get(name=STATE["resource"])
    # The plugin reports user_id as user.id when it is an Entra objectId (GUID).
    session = await agent.async_create_session(user_id=user_id)
    session_id = session["id"] if isinstance(session, dict) else session.id
    print(f"session {session_id}  user {user_id}")
    async for event in agent.async_stream_query(user_id=user_id, session_id=session_id, message=message):
        for part in (event.get("content") or {}).get("parts") or []:
            if part.get("function_call"):
                print(f"  -> tool {part['function_call'].get('name')}")
            elif part.get("text") and event.get("author") != "user":
                print(part["text"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("message", nargs="?", default="Assess renewal risk for Contoso")
    parser.add_argument("--user-id", default=CONFIG.get("testUserId", ""),
                        help="Entra objectId of the caller (default: testUserId in config.json)")
    args = parser.parse_args()
    asyncio.run(run(args.message, args.user_id))


if __name__ == "__main__":
    main()
