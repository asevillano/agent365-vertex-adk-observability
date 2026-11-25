"""GCP credentials for the local scripts.

Application Default Credentials first; if they are missing or need reauthentication
(`gcloud auth application-default login`), fall back to a short-lived access token from the
active gcloud account.
"""
import shutil
import subprocess

import google.auth
import google.auth.exceptions
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _gcloud_token_credentials(reason: Exception) -> Credentials:
    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        raise reason
    print(f"ADC unavailable ({type(reason).__name__}); using the gcloud account token (valid ~1 h).")
    token = subprocess.run([gcloud, "auth", "print-access-token"], check=True,
                           capture_output=True, text=True).stdout.strip()
    return Credentials(token=token)


def gcp_credentials():
    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
        credentials.refresh(Request())
        return credentials
    except google.auth.exceptions.GoogleAuthError as exc:
        return _gcloud_token_credentials(exc)


def patch_google_auth_default(project: str) -> None:
    """For local runs only: make libraries that call google.auth.default() use gcp_credentials()."""
    credentials = gcp_credentials()
    google.auth.default = lambda *args, **kwargs: (credentials, project)
