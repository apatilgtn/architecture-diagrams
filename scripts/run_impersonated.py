"""Run the real `arch-diagram-mcp` HTTP server locally as a GCP service account, without a key file.

Application Default Credentials are replaced by impersonated credentials for GCP_TARGET_SA, sourced from
a short-lived user token, so nothing is written to the machine's ADC and no service-account key exists.
The caller needs roles/iam.serviceAccountTokenCreator on the service account.

usage (PowerShell):
  $env:GCP_SOURCE_TOKEN = gcloud auth print-access-token
  $env:GCP_TARGET_SA = "<sa-name>@<project-id>.iam.gserviceaccount.com"
  $env:GCP_PROJECT = "<project-id>"
  python scripts/run_impersonated.py      # plus the usual ARCH_DIAGRAM_* settings

See docs/CLOUD_TESTING.md.
"""
import os
import sys

import google.auth
from google.auth import impersonated_credentials
from google.oauth2.credentials import Credentials

source = Credentials(token=os.environ["GCP_SOURCE_TOKEN"])
target = impersonated_credentials.Credentials(
    source_credentials=source,
    target_principal=os.environ["GCP_TARGET_SA"],
    target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    lifetime=3600,
)
project = os.environ["GCP_PROJECT"]
google.auth.default = lambda *a, **k: (target, project)

from arch_diagram_mcp.__main__ import main  # noqa: E402

sys.exit(main(["--transport", "http"]))
