"""Sample renewal-risk agent (Google ADK) with simulated data.

It only exists to show the Agent 365 instrumentation end to end: four function tools over an
in-memory dataset, so it runs anywhere with access to Gemini on Vertex AI. Replace it with your
own agent; the observability plugin does not depend on it.

Settings (environment): GOOGLE_CLOUD_PROJECT (set by Agent Engine), GOOGLE_CLOUD_LOCATION,
AGENT_MODEL (default gemini-2.5-flash).
"""
import os

# The google-genai client behind ADK must target Vertex AI (ADC), not an API key.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

from google.adk.agents import Agent

MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")

_ACCOUNTS = {
    "ACC-001": {"name": "Contoso Ltd", "arr": 480000, "renewal_date": "2027-02-28", "auto_renew": False,
                "requested_discount_pct": 18},
    "ACC-002": {"name": "Fabrikam Inc", "arr": 210000, "renewal_date": "2027-05-31", "auto_renew": True,
                "requested_discount_pct": 0},
    "ACC-003": {"name": "Northwind Traders", "arr": 125000, "renewal_date": "2027-01-15", "auto_renew": False,
                "requested_discount_pct": 10},
}
_INCIDENTS = {
    "ACC-001": [{"number": "INC0012", "priority": "1 - Critical", "state": "Open", "opened": "2026-11-02",
                 "summary": "Data sync failures in EU region"},
                {"number": "INC0019", "priority": "3 - Moderate", "state": "Open", "opened": "2026-11-18",
                 "summary": "Slow report exports"}],
    "ACC-002": [{"number": "INC0007", "priority": "4 - Low", "state": "Resolved", "opened": "2026-09-12",
                 "summary": "SSO configuration question"}],
    "ACC-003": [{"number": "INC0015", "priority": "2 - High", "state": "Open", "opened": "2026-10-28",
                 "summary": "API rate limiting errors"}],
}
_USAGE = {  # monthly active users, last four months
    "ACC-001": [1180, 1040, 910, 760],
    "ACC-002": [530, 545, 560, 590],
    "ACC-003": [300, 290, 260, 255],
}
_CONTRACTS = {
    "ACC-001": "Annual term, no auto-renew, 60-day notice, renewal price cap 5%, discounts above 15% need Deal Desk.",
    "ACC-002": "Three-year term with auto-renew, 90-day notice, renewal price cap 3%.",
    "ACC-003": "Annual term, no auto-renew, 30-day notice, no price cap.",
}


def _find(name_or_id: str) -> tuple[str, dict] | tuple[None, None]:
    key = name_or_id.strip().lower()
    for account_id, account in _ACCOUNTS.items():
        if key in (account_id.lower(), account["name"].lower()) or key in account["name"].lower():
            return account_id, account
    return None, None


def get_account(name_or_id: str) -> dict:
    """Returns the CRM record of a customer: ARR, renewal date, auto-renew and requested discount."""
    account_id, account = _find(name_or_id)
    if not account:
        return {"error": f"No account matches '{name_or_id}'", "known_accounts": [a["name"] for a in _ACCOUNTS.values()]}
    return {"account_id": account_id, **account}


def list_incidents(account_id: str) -> dict:
    """Returns the support incidents of a customer (use the account_id from get_account)."""
    return {"account_id": account_id, "incidents": _INCIDENTS.get(account_id, [])}


def get_usage_trend(account_id: str) -> dict:
    """Returns monthly active users for the last four months and the change from first to last month."""
    series = _USAGE.get(account_id)
    if not series:
        return {"account_id": account_id, "error": "No usage data"}
    change = round((series[-1] - series[0]) / series[0] * 100, 1)
    return {"account_id": account_id, "monthly_active_users": series, "change_pct": change}


def get_contract_terms(account_id: str) -> dict:
    """Returns the renewal-relevant terms of the customer's contract."""
    return {"account_id": account_id, "terms": _CONTRACTS.get(account_id, "No contract on file")}


INSTRUCTION = """You are a renewal-risk analyst for a B2B SaaS vendor.
For the customer the user names: call get_account, then list_incidents, get_usage_trend and
get_contract_terms with its account_id. Score the renewal risk as Low, Medium or High, list the
signals behind the score (open critical incidents, declining usage, no auto-renew, discount above
the approval threshold) and recommend two or three next actions. If no customer is named, ask for
one. Ground every statement in tool output and be concise."""

root_agent = Agent(
    name="renewal_risk_sample",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[get_account, list_incidents, get_usage_trend, get_contract_terms],
)
