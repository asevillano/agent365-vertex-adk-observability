"""Agent Engine entrypoint: the ADK agent plus the Agent 365 observability plugin.

To instrument your own agent, change the `root_agent` import below (and AGENT_PACKAGE in deploy.py).
"""
import logging

from vertexai.agent_engines import AdkApp

from a365_observability import A365ObservabilityPlugin
from sample_agent.agent import root_agent

_log = logging.getLogger("a365_observability")
_log.setLevel(logging.INFO)
if not _log.handlers:
    # export results must reach Cloud Logging even if the runtime leaves the root logger at WARNING
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_handler)
    _log.propagate = False

plugin = A365ObservabilityPlugin.from_env()
app = AdkApp(agent=root_agent, plugins=[plugin])
