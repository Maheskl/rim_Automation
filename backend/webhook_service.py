#!/usr/bin/env python3
# webhook_service.py
import os, logging, threading
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# Reuse functions from app.py
from app import (
    jira_get_issue,
    extract_field_value,
    normalize_jira_time,
    iso_to_epoch_ms,
    extract_robot_num,
    extract_platform,
    build_grafana_url,
    jira_post_comment,
)

logging.basicConfig(level=logging.INFO)
app = FastAPI()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
JIRA_BASE = os.environ.get("JIRA_BASE")
JIRA_USER = os.environ.get("JIRA_USER")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN")
PRODUCT_FIELD_ID = os.environ.get("PRODUCT_FIELD_ID", "customfield_11675")
TIME_FIELD_ID = os.environ.get("TIME_FIELD_ID", "customfield_11609")
GRAFANA_HOST = os.environ.get("GRAFANA_HOST", "grafana.core.dev.hmnd.ai")
GRAFANA_UID = os.environ.get("GRAFANA_UID", "13b4a1f2-b2be-47d2-b6de-29951daebc54")
GRAFANA_SLUG = os.environ.get("GRAFANA_SLUG", "alpha-robot-overview")
PRE_MS = int(os.environ.get("PRE_MS", "120000"))
POST_MS = int(os.environ.get("POST_MS", "60000"))

class WebhookPayload(BaseModel):
    issueKey: str
    affected_product: str | None = None
    timing: str | None = None
    include_rosbag: str | None = None

def process_issue(payload: WebhookPayload):
    issue = payload.issueKey
    try:
        # prefer payload values, otherwise fetch from Jira
        if payload.affected_product and payload.timing:
            product = payload.affected_product
            timing_raw = payload.timing
        else:
            issue_json = jira_get_issue(JIRA_BASE, JIRA_USER, JIRA_TOKEN, issue)
            fields = issue_json.get("fields", {})
            product = extract_field_value(fields.get(PRODUCT_FIELD_ID))
            timing_raw = fields.get(TIME_FIELD_ID) or ""

        timing_iso = normalize_jira_time(timing_raw) or ""
        incident_ms = iso_to_epoch_ms(timing_iso) if timing_iso else int(__import__("time").time()*1000)
        from_ms = incident_ms - PRE_MS
        to_ms = incident_ms + POST_MS

        robot = extract_robot_num(product)
        platform = extract_platform(product)
        grafana_url = build_grafana_url(GRAFANA_HOST, GRAFANA_UID, GRAFANA_SLUG,
                                        from_ms, to_ms, platform, robot)

        # post final comment (ADF)
        jira_post_comment(JIRA_BASE, JIRA_USER, JIRA_TOKEN, issue, product, robot, timing_iso or str(incident_ms), grafana_url)
        logging.info("Processed %s OK", issue)
    except Exception as e:
        logging.exception("Worker failed for %s: %s", issue, e)
        # post failure comment
        try:
            jira_post_comment(JIRA_BASE, JIRA_USER, JIRA_TOKEN, issue,
                              product if 'product' in locals() else "(unknown)",
                              robot if 'robot' in locals() else "(unknown)",
                              timing_iso if 'timing_iso' in locals() else "(unknown)",
                              f"Failed to create RIM snapshot: {e}")
        except Exception:
            logging.exception("Failed to post error comment for %s", issue)

@app.post("/jira/webhook")
async def jira_webhook(payload: WebhookPayload, x_webhook_secret: str | None = Header(None)):
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    logging.info("Received webhook for %s", payload.issueKey)
    # immediate ack
    threading.Thread(target=process_issue, args=(payload,), daemon=True).start()
    return {"status": "accepted", "issue": payload.issueKey}