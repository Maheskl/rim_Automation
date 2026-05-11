# RIM Automation

Two integrations sharing one FastAPI service:

1. **Grafana snapshot links** — Jira webhook generates a Grafana URL with the incident time window and posts it back as a Jira comment.
2. **Slack → Jira attachment carry-over** — Slack message shortcuts let users create RIM issues (or attach files to existing issues) directly from Slack, with photos and videos uploaded automatically.

## Contents

- `backend/app.py` — CLI for the Grafana flow (manual usage).
- `backend/webhook_service.py` — FastAPI app. Mounts `/jira/webhook` (Grafana flow) and the Slack router.
- `backend/slack_handler.py` — Slack interactive endpoint, modal builders, background workers.
- `backend/jira_issue_creator.py` — Reporter mapping (Slack user → email → Jira accountId) and issue creation.
- `backend/attachment_pipeline.py` — Slack file download + Jira attachment upload.
- `jira_test.sh` — Local test script.

## Requirements

Python 3.10+.

```bash
pip install -r backend/requirements.txt
```

## Environment variables

### Shared
- `JIRA_BASE` (e.g., `https://yourorg.atlassian.net`)
- `JIRA_USER`
- `JIRA_TOKEN`

### Grafana flow
- `PRODUCT_FIELD_ID` (default: `customfield_11675`)
- `TIME_FIELD_ID` (default: `customfield_11609`)
- `GRAFANA_HOST` (default: `grafana.core.dev.hmnd.ai`)
- `GRAFANA_UID`
- `GRAFANA_SLUG`
- `PRE_MS` (default: `120000`)
- `POST_MS` (default: `60000`)
- `WEBHOOK_SECRET` (optional, for webhook authentication)

### Slack flow
- `SLACK_BOT_TOKEN` (Bot User OAuth token, starts with `xoxb-`)
- `SLACK_SIGNING_SECRET` (from the Slack app's Basic Information page)
- `JIRA_DEFAULT_PROJECT` (default: `RIM`)
- `JIRA_DEFAULT_ISSUE_TYPE` (default: `Robot Issue`)
- `JIRA_FALLBACK_ACCOUNT_ID` (Jira accountId used as reporter when mapping fails)

## CLI usage (Grafana flow)

```bash
python backend/app.py --issue RIM-319
python backend/app.py --issue RIM-319 --post
```

## Running the service

```bash
uvicorn backend.webhook_service:app --host 0.0.0.0 --port 8000 --env-file backend/.env
```

## Endpoints

### `POST /jira/webhook` — Grafana flow

```json
{
  "issueKey": "RIM-319",
  "affected_product": "Alpha 1.0 #12 (Wheeled)",
  "timing": "2025-12-17T17:00:00.000+0000"
}
```

Include header `X-Webhook-Secret` if `WEBHOOK_SECRET` is set.

### `POST /slack/interactive` — Slack flow

Receives all interactive payloads from the **humanoid-jira-bridge** Slack app. Every request is HMAC-verified against `SLACK_SIGNING_SECRET`. The handler dispatches on `payload.type`:

- `message_action` — opens a modal for the triggered shortcut
- `view_submission` — runs the work in a background thread, returns immediately

Two message shortcuts:

| Shortcut name | Callback ID | What it does |
|---|---|---|
| Create Jira issue with attachments | `create_jira_with_attachments` | Modal collects summary, reporter, robot, priority. Creates a new RIM-xxxx issue and attaches the message's files. |
| Attach files to Jira issue | `attach_to_jira_issue` | Modal asks for an existing Jira key. Attaches the message's files to it. |

### Slack app setup (one-time)

1. Create a Slack app at `api.slack.com/apps`.
2. **Bot Token Scopes**: `commands`, `files:read`, `chat:write`, `channels:history`, `groups:history`, `users:read`, `users:read.email`, `reactions:write`.
3. **Interactivity & Shortcuts**: enable, set Request URL to `https://<your-host>/slack/interactive`, register the two message shortcuts above with the matching callback IDs.
4. Install to workspace; copy the Bot Token and Signing Secret into `.env`.
5. Invite the bot to every channel where it should be used: `/invite @humanoid-jira-bridge`.
