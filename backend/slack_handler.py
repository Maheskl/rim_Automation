import os
import json
import hmac
import hashlib
import time
import logging
import threading
import requests
from fastapi import APIRouter, Request, HTTPException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from attachment_pipeline import attach_to_jira, collect_files, is_duplicate_trigger
from jira_issue_creator import create_issue, resolve_reporter

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
JIRA_BASE = os.environ.get("JIRA_BASE", "")
JIRA_USER = os.environ.get("JIRA_USER", "")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
JIRA_DEFAULT_PROJECT = os.environ.get("JIRA_DEFAULT_PROJECT", "HA")

slack_router = APIRouter()
slack_client = WebClient(token=SLACK_BOT_TOKEN)


def verify_slack_signature(body_bytes: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        logging.warning("SLACK_SIGNING_SECRET not set — skipping verification (unsafe)")
        return True

    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    base = f"v0:{timestamp}:{body_bytes.decode('utf-8')}".encode("utf-8")
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@slack_router.post("/slack/interactive")
async def slack_interactive(request: Request):
    body_bytes = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body_bytes, timestamp, signature):
        raise HTTPException(status_code=403, detail="invalid Slack signature")

    form = await request.form()
    try:
        payload = json.loads(form["payload"])
    except (KeyError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"bad payload: {e}")

    event_type = payload.get("type")

    if event_type == "message_action":
        return _handle_shortcut(payload)
    if event_type == "view_submission":
        return _handle_view_submission(payload)
    return {}


def _handle_shortcut(payload: dict):
    callback_id = payload.get("callback_id", "")
    trigger_id = payload.get("trigger_id", "")

    if is_duplicate_trigger(trigger_id):
        logging.info("Duplicate trigger %s — ignoring", trigger_id)
        return {}

    message = payload.get("message", {})
    channel_id = payload.get("channel", {}).get("id", "")
    slack_user_id = payload.get("user", {}).get("id", "")

    private_metadata = json.dumps({
        "channel_id": channel_id,
        "message_ts": message.get("ts", ""),
        "slack_user_id": slack_user_id,
        "slack_permalink": _get_permalink(channel_id, message.get("ts", "")),
    })

    prefill = (message.get("text") or "")[:100]

    if callback_id == "create_jira_with_attachments":
        modal = _build_create_issue_modal(prefill, private_metadata)
    elif callback_id == "attach_to_jira_issue":
        modal = _build_attach_issue_modal(private_metadata)
    else:
        logging.warning("Unknown callback_id: %s", callback_id)
        return {}

    try:
        slack_client.views_open(trigger_id=trigger_id, view=modal)
    except SlackApiError as e:
        logging.error("Failed to open modal: %s", e)

    return {}


def _handle_view_submission(payload: dict):
    callback_id = payload.get("view", {}).get("callback_id", "")
    state_values = payload.get("view", {}).get("state", {}).get("values", {})
    private_metadata = json.loads(payload.get("view", {}).get("private_metadata", "{}"))

    if callback_id == "create_jira_modal":
        summary = state_values.get("summary_block", {}).get("summary_input", {}).get("value", "")
        project = state_values.get("project_block", {}).get("project_input", {}).get("value", JIRA_DEFAULT_PROJECT)
        issue_type = state_values.get("type_block", {}).get("type_select", {}).get("selected_option", {}).get("value", "Task")

        threading.Thread(
            target=_worker_create_and_attach,
            args=(summary, project, issue_type, private_metadata),
            daemon=True,
        ).start()

    elif callback_id == "attach_jira_modal":
        issue_key = state_values.get("issue_block", {}).get("issue_input", {}).get("value", "").strip().upper()
        threading.Thread(
            target=_worker_attach_only,
            args=(issue_key, private_metadata),
            daemon=True,
        ).start()

    return {"response_action": "clear"}


def _worker_create_and_attach(summary: str, project: str, issue_type: str, meta: dict):
    channel_id = meta["channel_id"]
    message_ts = meta["message_ts"]
    slack_user_id = meta["slack_user_id"]
    slack_permalink = meta.get("slack_permalink", "")

    try:
        resp = slack_client.conversations_history(
            channel=channel_id, latest=message_ts, limit=1, inclusive=True
        )
        message = resp["messages"][0] if resp.get("messages") else {}
        files = collect_files(message)

        reporter_id = resolve_reporter(
            slack_user_id, slack_client,
            jira_base=JIRA_BASE, jira_auth=(JIRA_USER, JIRA_TOKEN),
        )

        issue_key = create_issue(
            summary=summary,
            slack_permalink=slack_permalink,
            reporter_account_id=reporter_id,
            project_key=project,
            issue_type=issue_type,
        )

        result = attach_to_jira(
            issue_key=issue_key,
            files=files,
            slack_token=SLACK_BOT_TOKEN,
            jira_base=JIRA_BASE,
            jira_auth=(JIRA_USER, JIRA_TOKEN),
        )

        jira_url = f"{JIRA_BASE.rstrip('/')}/browse/{issue_key}"
        n_attached = len(result["attached"])
        n_skipped = len(result["skipped"])
        n_failed = len(result["failed"])

        msg = f":white_check_mark: Created <{jira_url}|{issue_key}> — {n_attached} file(s) attached"
        if n_skipped:
            msg += f", {n_skipped} external link(s) skipped"
        if n_failed:
            msg += f", {n_failed} failed"

        slack_client.chat_postMessage(channel=channel_id, thread_ts=message_ts, text=msg)
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")

        if result["skipped"]:
            _post_skipped_links_comment(issue_key, result["skipped"])
        if result["failed"]:
            _post_failure_comment(issue_key, result["failed"])

    except Exception as e:
        logging.exception("create+attach failed for message %s: %s", message_ts, e)
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=message_ts,
            text=f":x: Failed to create Jira issue: {e}",
        )


def _worker_attach_only(issue_key: str, meta: dict):
    channel_id = meta["channel_id"]
    message_ts = meta["message_ts"]

    try:
        resp = slack_client.conversations_history(
            channel=channel_id, latest=message_ts, limit=1, inclusive=True
        )
        message = resp["messages"][0] if resp.get("messages") else {}
        files = collect_files(message)

        if not files:
            slack_client.chat_postMessage(
                channel=channel_id, thread_ts=message_ts,
                text=f":information_source: No files on this message to attach to {issue_key}.",
            )
            return

        result = attach_to_jira(
            issue_key=issue_key,
            files=files,
            slack_token=SLACK_BOT_TOKEN,
            jira_base=JIRA_BASE,
            jira_auth=(JIRA_USER, JIRA_TOKEN),
        )

        jira_url = f"{JIRA_BASE.rstrip('/')}/browse/{issue_key}"
        n = len(result["attached"])
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=message_ts,
            text=f":white_check_mark: Attached {n} file(s) to <{jira_url}|{issue_key}>",
        )
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")

        if result["failed"]:
            _post_failure_comment(issue_key, result["failed"])

    except Exception as e:
        logging.exception("attach-only failed for %s: %s", issue_key, e)
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=message_ts,
            text=f":x: Failed to attach files to {issue_key}: {e}",
        )


def _build_create_issue_modal(prefill_summary: str, private_metadata: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "create_jira_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Create Jira Issue"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "summary_block",
                "label": {"type": "plain_text", "text": "Summary"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "summary_input",
                    "initial_value": prefill_summary,
                    "placeholder": {"type": "plain_text", "text": "Brief description"},
                },
            },
            {
                "type": "input",
                "block_id": "project_block",
                "label": {"type": "plain_text", "text": "Project Key"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "project_input",
                    "initial_value": JIRA_DEFAULT_PROJECT,
                    "placeholder": {"type": "plain_text", "text": "e.g. HA"},
                },
            },
            {
                "type": "input",
                "block_id": "type_block",
                "label": {"type": "plain_text", "text": "Issue Type"},
                "element": {
                    "type": "static_select",
                    "action_id": "type_select",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "Task"},
                        "value": "Task",
                    },
                    "options": [
                        {"text": {"type": "plain_text", "text": "Task"}, "value": "Task"},
                        {"text": {"type": "plain_text", "text": "Bug"}, "value": "Bug"},
                        {"text": {"type": "plain_text", "text": "Incident"}, "value": "Incident"},
                    ],
                },
            },
        ],
    }


def _build_attach_issue_modal(private_metadata: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "attach_jira_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Attach to Jira Issue"},
        "submit": {"type": "plain_text", "text": "Attach"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "issue_block",
                "label": {"type": "plain_text", "text": "Jira Issue Key"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "issue_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. HA-1234"},
                },
            },
        ],
    }


def _get_permalink(channel_id: str, message_ts: str) -> str:
    try:
        resp = slack_client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return resp.get("permalink", "")
    except SlackApiError:
        return ""


def _post_failure_comment(issue_key: str, failed: list[dict]):
    reasons = "; ".join(f"{f['name']}: {f['reason']}" for f in failed)
    _post_jira_comment(issue_key, f"Warning: some files could not be attached: {reasons}")


def _post_skipped_links_comment(issue_key: str, skipped: list[dict]):
    lines = "\n".join(f"- {s['name']}: {s['url']}" for s in skipped)
    _post_jira_comment(issue_key, f"External file links (could not be downloaded):\n{lines}")


def _post_jira_comment(issue_key: str, text: str):
    url = f"{JIRA_BASE.rstrip('/')}/rest/api/3/issue/{issue_key}/comment"
    body = {
        "body": {
            "type": "doc", "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }],
        }
    }
    requests.post(
        url, json=body, auth=(JIRA_USER, JIRA_TOKEN),
        headers={"Content-Type": "application/json"}, timeout=15,
    )
