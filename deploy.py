"""Deploy the instrumented ADK agent to Vertex AI Agent Engine (create or update).

Source deployment (no staging bucket): the agent package, the Agent 365 plugin, the entrypoint and
the pinned requirements. It also grants the runtime service account read access to the blueprint
secret only.

Without --apply it only checks and prints the plan; nothing changes.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile

from google.auth.transport.requests import AuthorizedSession

from gcp_auth import gcp_credentials

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "agent"
CONFIG = json.loads((HERE / "config.json").read_text("utf-8-sig"))
STATE_FILE = HERE / "deployment.json"

PROJECT = CONFIG["gcpProject"]
LOCATION = CONFIG["gcpRegion"]
DISPLAY_NAME = CONFIG["displayName"]
SERVICE_ACCOUNT = CONFIG["serviceAccount"]
SECRET_ROLE = "roles/secretmanager.secretAccessor"
AGENT_PACKAGE = "sample_agent"  # the package imported by agent/a365_entrypoint.py
PACKAGES = [AGENT_PACKAGE, "a365_observability.py", "a365_entrypoint.py", "requirements.txt"]


def agent_env() -> dict:
    return {
        **CONFIG.get("agentEnv", {}),
        "A365_TENANT_ID": CONFIG["tenantId"],
        "A365_AGENT_APP_ID": CONFIG["agentAppId"],
        "A365_BLUEPRINT_ID": CONFIG.get("agentBlueprintId", ""),
        "A365_AGENT_NAME": CONFIG["agentName"],
        "A365_AGENT_DESCRIPTION": CONFIG.get("agentDescription", ""),
        "A365_CHANNEL": CONFIG.get("channel", "web"),
        "A365_SERVICE_NAME": DISPLAY_NAME,
        "A365_SECRET_NAME": CONFIG["secretName"],
        "A365_SECRET_PROJECT": PROJECT,
    }


def operation_schemas() -> list[dict]:
    schemas = []
    for name, mode, fields in (
        ("async_create_session", "async", ["user_id"]),
        ("async_delete_session", "async", ["user_id", "session_id"]),
        ("async_stream_query", "async_stream", ["user_id", "session_id", "message"]),
    ):
        schemas.append({
            "name": name,
            "api_mode": mode,
            "parameters": {
                "type": "object",
                "properties": {field: {"type": "string"} for field in fields},
                "required": fields,
            },
        })
    return schemas


@contextmanager
def staged_source():
    """Copy only the source files; never out/, caches or local settings."""
    with tempfile.TemporaryDirectory(prefix="a365-adk-") as directory:
        stage = Path(directory)
        shutil.copytree(SOURCE / AGENT_PACKAGE, stage / AGENT_PACKAGE,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"))
        for name in PACKAGES[1:]:
            shutil.copyfile(SOURCE / name, stage / name)
        yield stage


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {"project": PROJECT, "location": LOCATION, "display_name": DISPLAY_NAME}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def existing_deployment(session: AuthorizedSession, state: dict) -> str:
    """Resource name if this script already deployed it; raises on a foreign agent with our name."""
    url = (f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}"
           f"/locations/{LOCATION}/reasoningEngines")
    matching, token = [], ""
    while True:
        response = session.get(url, params={"pageSize": 100, "pageToken": token}, timeout=30)
        response.raise_for_status()
        listing = response.json()
        matching += [a["name"] for a in listing.get("reasoningEngines", []) if a.get("displayName") == DISPLAY_NAME]
        token = listing.get("nextPageToken", "")
        if not token:
            break
    if not matching:
        return ""
    if matching == [state.get("resource")]:
        return matching[0]
    raise RuntimeError(f"An agent named {DISPLAY_NAME} already exists and was not deployed from here: "
                       f"{', '.join(matching)}. Change displayName in config.json.")


def check_service_account(session: AuthorizedSession) -> None:
    url = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{SERVICE_ACCOUNT}"
    response = session.get(url, timeout=30)
    if response.status_code == 404:
        raise RuntimeError(f"Service account {SERVICE_ACCOUNT} does not exist; create it first (README, step 7)")
    response.raise_for_status()
    if response.json().get("disabled"):
        raise RuntimeError(f"Service account {SERVICE_ACCOUNT} is disabled")


def secret_access(session: AuthorizedSession, apply: bool) -> str:
    """Grant the runtime identity read access to the blueprint secret only (idempotent)."""
    url = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/{CONFIG['secretName']}"
    response = session.get(url + ":getIamPolicy", params={"options.requestedPolicyVersion": 3}, timeout=30)
    if response.status_code == 404:
        raise RuntimeError(f"Secret {CONFIG['secretName']} does not exist; run store-blueprint-secret.ps1 first")
    response.raise_for_status()
    policy = response.json()
    member = f"serviceAccount:{SERVICE_ACCOUNT}"
    for binding in policy.setdefault("bindings", []):
        if binding["role"] == SECRET_ROLE and not binding.get("condition") and member in binding["members"]:
            return "already granted"
    if not apply:
        return "would grant"
    binding = next((b for b in policy["bindings"] if b["role"] == SECRET_ROLE and not b.get("condition")), None)
    if binding:
        binding["members"].append(member)
    else:
        policy["bindings"].append({"role": SECRET_ROLE, "members": [member]})
    response = session.post(url + ":setIamPolicy", json={"policy": policy}, timeout=30)
    response.raise_for_status()
    return "granted"


def engine_config(env: dict) -> dict:
    return {
        "source_packages": PACKAGES,
        "entrypoint_module": "a365_entrypoint",
        "entrypoint_object": "app",
        "requirements_file": "requirements.txt",
        "agent_framework": "google-adk",
        "python_version": "3.12",
        "class_methods": operation_schemas(),
        "display_name": DISPLAY_NAME,
        "description": "ADK agent instrumented for Microsoft Agent 365 observability.",
        "labels": {"integration": "agent365-observability"},
        "service_account": SERVICE_ACCOUNT,
        "env_vars": env,
        "min_instances": 0,
        "max_instances": 1,
        "resource_limits": {"cpu": "1", "memory": "2Gi"},
        "container_concurrency": 3,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="grant the secret access and create or update the Agent Engine (may incur cloud costs)")
    args = parser.parse_args()

    state = load_state()
    credentials = gcp_credentials()
    session = AuthorizedSession(credentials)
    check_service_account(session)
    current = existing_deployment(session, state)
    env = agent_env()

    print(f"Project/region : {PROJECT} / {LOCATION}")
    print(f"Agent          : {DISPLAY_NAME} ({'update: ' + current if current else 'to create'})")
    print(f"Runtime SA     : {SERVICE_ACCOUNT}")
    print(f"Secret access  : {SECRET_ROLE} on {CONFIG['secretName']} -> {secret_access(session, False)}")
    print(f"Source         : {', '.join(PACKAGES)}")
    print(f"Environment    : {', '.join(sorted(env))}")
    if not args.apply:
        print("\nPlan only. Re-run with --apply to make these changes.")
        return

    print(f"Secret access  : {secret_access(session, True)}")
    import vertexai

    client = vertexai.Client(project=PROJECT, location=LOCATION, credentials=credentials)
    with staged_source() as directory:
        previous = Path.cwd()
        os.chdir(directory)
        try:
            if current:
                remote = client.agent_engines.update(name=current, config=engine_config(env))
            else:
                remote = client.agent_engines.create(config=engine_config(env))
        finally:
            os.chdir(previous)
    if not remote.api_resource or not remote.api_resource.name:
        raise RuntimeError("Agent Engine deployment returned no resource")
    state["resource"] = remote.api_resource.name
    save_state(state)
    print("Updated:" if current else "Created:", state["resource"])


if __name__ == "__main__":
    main()
