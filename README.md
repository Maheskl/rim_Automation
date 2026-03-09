# RIM Automation

Generate Grafana snapshot links from Jira issues and optionally post them back as comments.

## Contents

- `backend/app.py`: CLI to fetch Jira issue, compute Grafana URL, optionally post comment.
- `backend/webhook_service.py`: FastAPI webhook that accepts Jira payloads and posts Grafana link comments.
- `jira_test.sh`: Local test script.

## Requirements

Python 3.10+ recommended.

Install dependencies:

```bash
pip install -r backend/requirements.txt
```

## Environment variables

Set these before running:

- `JIRA_BASE` (e.g., `https://yourorg.atlassian.net`)
- `JIRA_USER`
- `JIRA_TOKEN`
- `PRODUCT_FIELD_ID` (default: `customfield_11675`)
- `TIME_FIELD_ID` (default: `customfield_11609`)
- `GRAFANA_HOST` (default: `grafana.core.dev.hmnd.ai`)
- `GRAFANA_UID`
- `GRAFANA_SLUG`
- `PRE_MS` (default: `120000`)
- `POST_MS` (default: `60000`)
- `WEBHOOK_SECRET` (optional, for webhook authentication)

## CLI usage

```bash
python backend/app.py --issue RIM-319
python backend/app.py --issue RIM-319 --post
```

## Webhook usage

Run the service:

```bash
uvicorn backend.webhook_service:app --host 0.0.0.0 --port 8000
```

POST to:

```
POST /jira/webhook
```

Example payload:

```json
{
  "issueKey": "RIM-319",
  "affected_product": "alpha #12",
  "timing": "2025-12-17T17:00:00.000+0000"
}
```

Include header `X-Webhook-Secret` if `WEBHOOK_SECRET` is set.
