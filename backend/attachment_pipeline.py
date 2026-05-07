import os
import time
import logging
import requests
from requests_toolbelt import MultipartEncoder

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
JIRA_BASE = os.environ.get("JIRA_BASE", "")
JIRA_USER = os.environ.get("JIRA_USER", "")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")

_seen_triggers: dict[str, float] = {}
_DEDUP_TTL = 30.0


def is_duplicate_trigger(trigger_id: str) -> bool:
    now = time.monotonic()
    stale = [k for k, v in _seen_triggers.items() if now - v > _DEDUP_TTL]
    for k in stale:
        del _seen_triggers[k]
    if trigger_id in _seen_triggers:
        return True
    _seen_triggers[trigger_id] = now
    return False


def collect_files(message: dict) -> list[dict]:
    return list(message.get("files", []))


def attach_to_jira(
    issue_key: str,
    files: list[dict],
    slack_token: str | None = None,
    jira_base: str | None = None,
    jira_auth: tuple | None = None,
) -> dict:
    """Returns {"attached": [filenames], "skipped": [...], "failed": [...]}"""
    slack_token = slack_token or SLACK_BOT_TOKEN
    jira_base = (jira_base or JIRA_BASE).rstrip("/")
    jira_auth = jira_auth or (JIRA_USER, JIRA_TOKEN)

    attached, skipped, failed = [], [], []

    for f in files:
        name = f.get("name", "file")
        mimetype = f.get("mimetype", "application/octet-stream")

        if f.get("is_external"):
            skipped.append({"name": name, "url": f.get("permalink", "")})
            logging.info("Skipping external file: %s", name)
            continue

        download_url = f.get("url_private_download") or f.get("url_private")
        if not download_url:
            failed.append({"name": name, "reason": "no download URL in file object"})
            continue

        try:
            _stream_file_to_jira(
                download_url=download_url,
                name=name,
                mimetype=mimetype,
                issue_key=issue_key,
                slack_token=slack_token,
                jira_base=jira_base,
                jira_auth=jira_auth,
            )
            attached.append(name)
            logging.info("Attached %s to %s", name, issue_key)
        except Exception as e:
            failed.append({"name": name, "reason": str(e)})
            logging.warning("Failed to attach %s to %s: %s", name, issue_key, e)

    return {"attached": attached, "skipped": skipped, "failed": failed}


def _stream_file_to_jira(
    download_url: str,
    name: str,
    mimetype: str,
    issue_key: str,
    slack_token: str,
    jira_base: str,
    jira_auth: tuple,
):
    upload_url = f"{jira_base}/rest/api/3/issue/{issue_key}/attachments"

    # X-Atlassian-Token: no-check bypasses Jira's CSRF check for attachment uploads
    jira_headers = {"X-Atlassian-Token": "no-check"}

    with requests.get(
        download_url,
        headers={"Authorization": f"Bearer {slack_token}"},
        stream=True,
        timeout=30,
    ) as slack_resp:
        slack_resp.raise_for_status()

        encoder = MultipartEncoder(
            fields={"file": (name, slack_resp.raw, mimetype)}
        )

        upload_resp = requests.post(
            upload_url,
            data=encoder,
            headers={**jira_headers, "Content-Type": encoder.content_type},
            auth=jira_auth,
            timeout=300,
        )
        upload_resp.raise_for_status()
