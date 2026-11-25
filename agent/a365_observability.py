"""Agent 365 observability for a Google ADK agent, sent over direct OTel (OTLP/HTTP+JSON, S2S).

Each ADK invocation becomes one run: an `invoke_agent` root span plus one `chat` span per
model call and one `execute_tool` span per tool call, all sharing the run's traceId.
Telemetry is best effort: any failure is logged and never reaches the agent.

Configuration (environment):
    A365_TENANT_ID, A365_AGENT_APP_ID, A365_AGENT_NAME     required (A365_AGENT_APP_ID = agent identity)
    A365_BLUEPRINT_ID                                      agent identity blueprint; when set, the secret is
                                                           the blueprint's and the token is obtained in two steps
    A365_SECRET_NAME                                       Secret Manager secret with the client secret
    A365_SECRET_PROJECT                                    defaults to GOOGLE_CLOUD_PROJECT
    A365_AGENT_DESCRIPTION, A365_CHANNEL (web), A365_SERVER_ADDRESS, A365_DEFAULT_USER_ID
    A365_SERVICE_NAME                                      service.name attribute (default: adk-agent)
    A365_DRY_RUN=1                                         build spans, log them, send nothing

Optional per-session state set by the caller: a365_user_email, a365_user_name, a365_client_ip.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from google.adk.plugins.base_plugin import BasePlugin

log = logging.getLogger("a365_observability")

RESOURCE_APP_ID = "9b975845-388f-4429-889e-eab1ef63949c"
BASE_URL = "https://agent365.svc.cloud.microsoft/observabilityService"
MAX_BODY_BYTES = 900_000  # service limit is 1 MB per request
MAX_MESSAGES_CHARS = 32_000
MAX_TOOL_CHARS = 16_000
_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

OK, ERROR = 1, 2
KIND_INTERNAL, KIND_CLIENT = 1, 3


def _hex(nbytes: int) -> str:
    return os.urandom(nbytes).hex()


def _clip_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    # keep the attribute valid JSON even when it has to be shortened
    return json.dumps(
        {"truncated": True, "originalChars": len(text), "preview": text[: limit - 200]},
        ensure_ascii=False,
    )


def _part_text(part: Any) -> str:
    if getattr(part, "text", None):
        return part.text
    call = getattr(part, "function_call", None)
    if call:
        return f"[function_call] {call.name}({json.dumps(call.args or {}, ensure_ascii=False, default=str)})"
    resp = getattr(part, "function_response", None)
    if resp:
        return f"[function_response] {resp.name}: {json.dumps(resp.response, ensure_ascii=False, default=str)}"
    return ""


def _content_message(content: Any, default_role: str) -> Optional[dict]:
    if content is None:
        return None
    text = "\n".join(t for t in (_part_text(p) for p in (content.parts or [])) if t)
    if not text:
        return None
    role = getattr(content, "role", None) or default_role
    return {"role": "assistant" if role == "model" else role, "content": text}


def _messages_json(messages: list[dict], limit: int = MAX_MESSAGES_CHARS) -> str:
    """Serialize messages, keeping the most recent ones when the history is too long."""
    msgs = list(messages)
    while msgs:
        text = json.dumps(msgs, ensure_ascii=False)
        if len(text) <= limit:
            return text
        if len(msgs) == 1:
            only = dict(msgs[0])
            only["content"] = only["content"][: limit // 2] + " …[truncated]"
            return json.dumps([only], ensure_ascii=False)
        msgs.pop(0)
    return "[]"


@dataclass
class _Span:
    span_id: str
    parent_id: str
    name: str
    kind: int
    start_ns: int
    attrs: dict = field(default_factory=dict)
    end_ns: int = 0
    status: int = OK
    status_message: str = ""


@dataclass
class _Run:
    trace_id: str
    root: _Span
    common: dict
    chats: list = field(default_factory=list)
    tools: dict = field(default_factory=dict)
    done: list = field(default_factory=list)
    partial_text: list = field(default_factory=list)
    final_texts: list = field(default_factory=list)
    model: str = ""


class _Exporter:
    """App-only token from a Secret Manager secret + OTLP/HTTP JSON POST.

    With a blueprint: blueprint secret -> T1 (fmi_path = agent identity) -> agent identity token.
    Without one: client credentials of a standard Entra app.
    """

    def __init__(self, tenant_id: str, agent_app_id: str, secret_name: str, secret_project: str,
                 base_url: str = BASE_URL, resource: str = RESOURCE_APP_ID, blueprint_id: str = ""):
        self.tenant_id, self.agent_app_id, self.blueprint_id = tenant_id, agent_app_id, blueprint_id
        self.secret_name, self.secret_project = secret_name, secret_project
        self.url = f"{base_url}/tenants/{tenant_id}/otlp/agents/{agent_app_id}/traces?api-version=1"
        self.resource = resource
        self._lock = threading.Lock()
        self._secret: Optional[str] = None
        self._token: Optional[str] = None
        self._token_exp = 0.0

    def _client_secret(self) -> str:
        if self._secret is None:
            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if not creds.valid:
                creds.refresh(google.auth.transport.requests.Request())
            url = (f"https://secretmanager.googleapis.com/v1/projects/{self.secret_project}"
                   f"/secrets/{self.secret_name}/versions/latest:access")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {creds.token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())["payload"]["data"]
            self._secret = base64.b64decode(data).decode("utf-8").strip()
        return self._secret

    def _token_request(self, form: dict) -> dict:
        req = urllib.request.Request(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                                     data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 300:
                return self._token
            scope = f"{self.resource}/.default"
            if self.blueprint_id:
                t1 = self._token_request({
                    "client_id": self.blueprint_id,
                    "client_secret": self._client_secret(),
                    "grant_type": "client_credentials",
                    "scope": "api://AzureADTokenExchange/.default",
                    "fmi_path": self.agent_app_id,
                })["access_token"]
                payload = self._token_request({
                    "client_id": self.agent_app_id,
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": t1,
                    "grant_type": "client_credentials",
                    "scope": scope,
                })
            else:
                payload = self._token_request({
                    "client_id": self.agent_app_id,
                    "client_secret": self._client_secret(),
                    "grant_type": "client_credentials",
                    "scope": scope,
                })
            self._token = payload["access_token"]
            self._token_exp = time.time() + int(payload.get("expires_in", 3600))
            return self._token

    def __call__(self, body: bytes, span_names: dict[str, str]) -> None:
        for attempt in range(3):
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    status, content = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, content = exc.code, exc.read()
                if status == 429 and attempt < 2:
                    time.sleep(float(exc.headers.get("Retry-After", "1")) + random.random())
                    continue
                if status == 401:
                    self._token = None
            break
        _log_results(status, content, span_names)


def _log_results(status: int, content: bytes, span_names: dict[str, str]) -> None:
    try:
        res = json.loads(content or b"{}")
    except ValueError:
        res = {}
    if status != 200:
        log.warning("Agent 365 export HTTP %s: %s", status, content[:500])
        return
    rejected = (res.get("partialSuccess") or {}).get("rejectedSpans")
    outcome = []
    for item in res.get("results") or []:
        sinks = item.get("sinks") or {}
        states = sorted({(s or {}).get("status", "?") + (f"({s.get('reason')})" if (s or {}).get("reason") else "")
                         for s in sinks.values()})
        outcome.append(f"{span_names.get(item.get('spanId'), item.get('spanId'))}={'/'.join(states)}")
    log.info("Agent 365 export HTTP 200 rejectedSpans=%s %s", rejected, " ".join(outcome))


class A365ObservabilityPlugin(BasePlugin):
    def __init__(self, *, tenant_id: str, agent_app_id: str, agent_name: str,
                 agent_description: str = "", channel: str = "web",
                 server_address: str = "", server_port: str = "443",
                 default_user_id: str = "", provider: str = "gcp.vertex_ai", blueprint_id: str = "",
                 service_name: str = "adk-agent",
                 sender: Optional[Callable[[bytes, dict], None]] = None):
        super().__init__(name="a365_observability")
        self.tenant_id, self.agent_app_id, self.agent_name = tenant_id, agent_app_id, agent_name
        self.blueprint_id = blueprint_id or agent_app_id
        self.service_name = service_name
        self.agent_description, self.channel = agent_description, channel
        self.server_address = server_address or "aiplatform.googleapis.com"
        self.server_port, self.default_user_id, self.provider = server_port, default_user_id, provider
        self.sender = sender
        self._runs: dict[str, _Run] = {}

    @classmethod
    def from_env(cls) -> "A365ObservabilityPlugin":
        env = os.environ
        region = env.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        sender = None
        blueprint_id = env.get("A365_BLUEPRINT_ID", "")
        if env.get("A365_DRY_RUN", "").lower() not in ("1", "true", "yes"):
            sender = _Exporter(
                tenant_id=env["A365_TENANT_ID"], agent_app_id=env["A365_AGENT_APP_ID"],
                secret_name=env["A365_SECRET_NAME"],
                secret_project=env.get("A365_SECRET_PROJECT") or env["GOOGLE_CLOUD_PROJECT"],
                blueprint_id=blueprint_id,
            )
        return cls(
            tenant_id=env["A365_TENANT_ID"], agent_app_id=env["A365_AGENT_APP_ID"],
            agent_name=env["A365_AGENT_NAME"], agent_description=env.get("A365_AGENT_DESCRIPTION", ""),
            channel=env.get("A365_CHANNEL", "web"),
            server_address=env.get("A365_SERVER_ADDRESS", f"{region}-aiplatform.googleapis.com"),
            default_user_id=env.get("A365_DEFAULT_USER_ID", ""), blueprint_id=blueprint_id,
            service_name=env.get("A365_SERVICE_NAME", "adk-agent"), sender=sender,
        )

    # ---- run -------------------------------------------------------------------------------

    async def before_run_callback(self, *, invocation_context):
        try:
            self._start_run(invocation_context)
        except Exception:  # noqa: BLE001 - telemetry must never break the agent
            log.exception("Agent 365: could not start the run")
        return None

    def _start_run(self, ctx) -> None:
        session = ctx.session
        state = getattr(session, "state", None) or {}
        user_key = str(getattr(session, "user_id", "") or "")
        user_id = user_key if _GUID.match(user_key) else (self.default_user_id or user_key or "unknown")
        common = {
            "microsoft.tenant.id": self.tenant_id,
            "gen_ai.agent.id": self.agent_app_id,
            "gen_ai.agent.name": self.agent_name,
            "microsoft.a365.agent.blueprint.id": self.blueprint_id,
            "user.id": user_id,
            "client.address": state.get("a365_client_ip") or "0.0.0.0",
            "gen_ai.conversation.id": session.id,
            "microsoft.session.id": session.id,
            "microsoft.channel.name": self.channel,
            "server.address": self.server_address,
            "server.port": self.server_port,
            "service.name": self.service_name,
            "operation.source": "adk-a365-plugin",
        }
        if self.agent_description:
            common["gen_ai.agent.description"] = self.agent_description
        if state.get("a365_user_email"):
            common["user.email"] = state["a365_user_email"]
        if state.get("a365_user_name"):
            common["user.name"] = state["a365_user_name"]
        user_msg = _content_message(ctx.user_content, "user")
        root = _Span(_hex(8), "", f"invoke_agent {self.agent_name}", KIND_INTERNAL, time.time_ns(), {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.execution.type": "HumanToAgent",
            "gen_ai.provider.name": self.provider,
            "gen_ai.input.messages": _messages_json([user_msg] if user_msg else []),
        })
        self._runs[ctx.invocation_id] = _Run(trace_id=_hex(16), root=root, common=common)

    async def on_event_callback(self, *, invocation_context, event):
        run = self._runs.get(invocation_context.invocation_id)
        try:
            if run and not getattr(event, "partial", False) and event.is_final_response():
                msg = _content_message(event.content, "assistant")
                if msg:
                    run.final_texts.append(msg["content"])
        except Exception:  # noqa: BLE001
            log.exception("Agent 365: could not read the event")
        return None

    async def after_run_callback(self, *, invocation_context):
        await self._finish(invocation_context.invocation_id)

    async def _finish(self, invocation_id: str, error: Optional[BaseException] = None) -> None:
        run = self._runs.pop(invocation_id, None)
        if not run:
            return
        try:
            now = time.time_ns()
            for span in run.chats + list(run.tools.values()):
                span.end_ns = span.end_ns or now
                if error and span.status == OK:
                    span.status, span.status_message = ERROR, "Run aborted"
                run.done.append(span)
            run.root.end_ns = now
            if run.model:
                run.root.attrs["gen_ai.request.model"] = run.model
            text = "\n\n".join(run.final_texts)
            run.root.attrs["gen_ai.output.messages"] = _messages_json(
                [{"role": "assistant", "content": text}] if text else [])
            if error:
                run.root.status, run.root.status_message = ERROR, str(error)[:500]
                run.root.attrs["error.type"] = type(error).__name__
            await asyncio.to_thread(self._export, run)
        except Exception:  # noqa: BLE001
            log.exception("Agent 365: export failed")

    # ---- model ---------------------------------------------------------------------------

    async def before_model_callback(self, *, callback_context, llm_request):
        run = self._runs.get(callback_context.invocation_id)
        if run:
            try:
                model = llm_request.model or ""
                run.model = run.model or model
                msgs = [m for m in (_content_message(c, "user") for c in llm_request.contents or []) if m]
                run.chats.append(_Span(_hex(8), run.root.span_id, f"chat {model}".strip(), KIND_CLIENT,
                                       time.time_ns(), {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": self.provider,
                    "gen_ai.request.model": model,
                    "gen_ai.input.messages": _messages_json(msgs),
                }))
            except Exception:  # noqa: BLE001
                log.exception("Agent 365: could not start the chat span")
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        run = self._runs.get(callback_context.invocation_id)
        span = run.chats[-1] if run and run.chats and not run.chats[-1].end_ns else None
        if span:
            try:
                msg = _content_message(llm_response.content, "assistant")
                if msg:
                    run.partial_text.append(msg["content"])
                if not getattr(llm_response, "partial", False):
                    self._close_chat(run, span, llm_response)
            except Exception:  # noqa: BLE001
                log.exception("Agent 365: could not close the chat span")
        return None

    def _close_chat(self, run: _Run, span: _Span, llm_response) -> None:
        span.end_ns = time.time_ns()
        text = "\n".join(run.partial_text)
        run.partial_text.clear()
        span.attrs["gen_ai.output.messages"] = _messages_json(
            [{"role": "assistant", "content": text}] if text else [])
        if llm_response.model_version:
            span.attrs["gen_ai.response.model"] = llm_response.model_version
        if llm_response.finish_reason:
            span.attrs["gen_ai.response.finish_reasons"] = json.dumps(
                [str(getattr(llm_response.finish_reason, "name", llm_response.finish_reason))])
        usage = llm_response.usage_metadata
        if usage is not None:
            if usage.prompt_token_count is not None:
                span.attrs["gen_ai.usage.input_tokens"] = usage.prompt_token_count
            if usage.candidates_token_count is not None:
                span.attrs["gen_ai.usage.output_tokens"] = usage.candidates_token_count
        if llm_response.error_code:
            span.status = ERROR
            span.status_message = f"{llm_response.error_code}: {llm_response.error_message or ''}"[:500]
        run.chats.remove(span)
        run.done.append(span)

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        run = self._runs.get(callback_context.invocation_id)
        if run and run.chats:
            span = run.chats[-1]
            span.status, span.status_message = ERROR, str(error)[:500]
            span.attrs["error.type"] = type(error).__name__
        # the error ends the run without after_run_callback
        await self._finish(callback_context.invocation_id, error)
        return None

    # ---- tools ---------------------------------------------------------------------------

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        run = self._runs.get(tool_context.invocation_id)
        if run:
            try:
                call_id = tool_context.function_call_id or str(uuid.uuid4())
                attrs = {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool.name,
                    "gen_ai.tool.call.id": call_id,
                    "gen_ai.tool.call.arguments": _clip_json(tool_args, MAX_TOOL_CHARS),
                    "gen_ai.tool.description": (tool.description or "").strip().split("\n")[0][:500],
                }
                server = _mcp_server(tool)
                if server:
                    attrs["gen_ai.tool.type"] = "MCP Server"
                    attrs["gen_ai.tool.server.name"] = server
                else:
                    attrs["gen_ai.tool.type"] = "function"
                run.tools[call_id] = _Span(_hex(8), run.root.span_id, f"execute_tool {tool.name}",
                                           KIND_CLIENT, time.time_ns(), attrs)
            except Exception:  # noqa: BLE001
                log.exception("Agent 365: could not start the tool span")
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        self._close_tool(tool_context, result=result)
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        self._close_tool(tool_context, error=error)
        await self._finish(tool_context.invocation_id, error)
        return None

    def _close_tool(self, tool_context, result: Any = None, error: Optional[BaseException] = None) -> None:
        run = self._runs.get(tool_context.invocation_id)
        span = run.tools.pop(tool_context.function_call_id, None) if run else None
        if not span:
            return
        try:
            span.end_ns = time.time_ns()
            if error is not None:
                span.status, span.status_message = ERROR, str(error)[:500]
                span.attrs["error.type"] = type(error).__name__
                span.attrs["gen_ai.tool.call.result"] = _clip_json({"error": str(error)}, MAX_TOOL_CHARS)
            else:
                span.attrs["gen_ai.tool.call.result"] = _clip_json(_jsonable(result), MAX_TOOL_CHARS)
            run.done.append(span)
        except Exception:  # noqa: BLE001
            log.exception("Agent 365: could not close the tool span")

    # ---- export ----------------------------------------------------------------------------

    def build_requests(self, run: _Run) -> list[tuple[bytes, dict]]:
        spans = [run.root] + sorted(run.done, key=lambda s: s.start_ns)
        encoded, names = [], {}
        for span in spans:
            attrs = {**run.common, **span.attrs}
            otlp = {
                "traceId": run.trace_id, "spanId": span.span_id, "parentSpanId": span.parent_id,
                "name": span.name, "kind": span.kind,
                "startTimeUnixNano": str(span.start_ns), "endTimeUnixNano": str(span.end_ns or span.start_ns),
                "status": {"code": span.status, **({"message": span.status_message} if span.status_message else {})},
                "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()],
            }
            encoded.append(json.dumps(otlp, ensure_ascii=False, separators=(",", ":")))
            names[span.span_id] = span.attrs["gen_ai.operation.name"] + (
                f":{span.attrs['gen_ai.tool.name']}" if "gen_ai.tool.name" in span.attrs else "")
        head = '{"resourceSpans":[{"scopeSpans":[{"scope":{"name":"adk-a365-plugin","version":"1.0.0"},"spans":['
        tail = "]}]}]}"
        requests_, batch = [], []
        for item in encoded:
            size = len((head + ",".join(batch + [item]) + tail).encode("utf-8"))
            if batch and size > MAX_BODY_BYTES:
                requests_.append(batch)
                batch = []
            batch.append(item)
        if batch:
            requests_.append(batch)
        return [((head + ",".join(b) + tail).encode("utf-8"), names) for b in requests_]

    def _export(self, run: _Run) -> None:
        for body, names in self.build_requests(run):
            if self.sender is None:
                log.info("Agent 365 dry run: %d bytes, spans %s", len(body), sorted(names.values()))
                continue
            try:
                self.sender(body, names)
            except Exception:  # noqa: BLE001
                log.exception("Agent 365: send failed")


def _mcp_server(tool: Any) -> str:
    """Hostname of the MCP server behind an ADK McpTool, or '' for function tools."""
    manager = getattr(tool, "_mcp_session_manager", None)
    params = getattr(manager, "_connection_params", None)
    url = getattr(params, "url", "") or ""
    return urllib.parse.urlsplit(url).hostname or ("mcp" if manager else "")


def _jsonable(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return value
