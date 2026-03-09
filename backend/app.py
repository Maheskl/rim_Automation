#!/usr/bin/env python3
"""
rim_grafana_link.py

Usage:
    # set env variables (or create a .env file) and run:
    python rim_grafana_link.py --issue RIM-319

Options:
    --post            : if set, post the constructed Grafana URL as a Jira comment
    --debug           : print debug info
    --issue ISSUE     : Jira issue key (overrides JIRA_ISSUE env)
"""

import os
import re
import sys
import json
import argparse
import logging
from typing import Any, Optional
from urllib.parse import quote_plus
import requests

# Optional dependency; used for robust ISO parsing
try:
    from dateutil.parser import isoparse
except Exception:
    isoparse = None


# Helper functions
def getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name, default)
    if v is not None and v.strip() == "":
        return default
    return v


def extract_field_value(field: Any) -> str:
    """
    Robustly return a string value for a Jira custom field which may be:
     - None
     - string
     - dict {value, name, id}
     - list [ {value|name|id}, ... ]
    """
    if field is None:
        return ""
    if isinstance(field, list):
        if len(field) == 0:
            return ""
        item = field[0]
        if isinstance(item, dict):
            return item.get("value") or item.get("name") or str(item.get("id", ""))
        return str(item)
    if isinstance(field, dict):
        return field.get("value") or field.get("name") or str(field.get("id", ""))
    return str(field)


def normalize_jira_time(raw: str) -> Optional[str]:
    """
    Convert Jira's time forms like:
      2025-12-17T17:00:00.000+0000
    ->  2025-12-17T17:00:00+00:00  (ISO acceptable by fromisoformat/isoparse)
    """
    if not raw:
        return None
    s = raw.strip()
    # common Jira form "2025-12-17T17:00:00.000+0000"
    if s.endswith("+0000"):
        if ".000" in s:
            s = s.replace(".000+0000", "+00:00")
        else:
            s = s[:-5] + "+00:00"
    # if ends with Z, it's OK
    return s


def iso_to_epoch_ms(iso_str: str) -> int:
    if not iso_str:
        return int(requests.utils.datetime_to_epoch(datetime=None) * 1000)  # fallback but not used
    # Try dateutil first if available
    if isoparse is not None:
        dt = isoparse(iso_str)
        return int(dt.timestamp() * 1000)
    # Fallback to fromisoformat (Python 3.7+)
    # Ensure iso_str has a colon in timezone like +00:00
    s = iso_str
    if s.endswith("+0000"):
        s = s.replace(".000+0000", "+00:00") if ".000" in s else s[:-5] + "+00:00"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception as e:
        raise ValueError(f"Unable to parse time string: {iso_str!r}") from e


def extract_robot_num(product_value: str) -> str:
    # look for "#<num>"
    m = re.search(r"#(\d+)", product_value)
    if m:
        return m.group(1)
    # fallback: first numeric token
    m2 = re.search(r"(\d+)", product_value)
    if m2:
        return m2.group(1)
    return "unknown"


def extract_platform(product_value: str) -> str:
    s = product_value.strip()
    if not s:
        return "unknown"
    platform = s.split()[0].lower()
    return platform


def build_grafana_url(host: str, uid: str, slug: str, from_ms: int, to_ms: int,
                      platform: str, robot: str, org_id: int = 1, timezone: str = "browser") -> str:
    platform_enc = quote_plus(platform)
    robot_enc = quote_plus(robot)
    url = (f"https://{host}/d/{uid}/{slug}?orgId={org_id}"
           f"&from={from_ms}&to={to_ms}&timezone={timezone}"
           f"&var-platform={platform_enc}&var-robot={robot_enc}")
    return url


def jira_get_issue(jira_base: str, user: str, token: str, issue_key: str) -> dict:
    url = f"{jira_base.rstrip('/')}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, auth=(user, token), headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def jira_post_comment(jira_base: str, user: str, token: str, issue_key: str, product: str, robot: str, timing_iso: str, grafana_url: str):
    """
    Post a Jira comment using ADF (safe for structured text and links).
    """
    url = f"{jira_base.rstrip('/')}/rest/api/3/issue/{issue_key}/comment"

    # Build ADF: two paragraphs
    body_adf = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": f"RIM snapshot for product {product} (robot {robot}) at {timing_iso}"}
                    ]
                },
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Grafana: "},
                        {
                            "type": "text",
                            "text": grafana_url,
                            "marks": [{"type": "link", "attrs": {"href": grafana_url}}]
                        }
                    ]
                }
            ]
        }
    }

    headers = {"Content-Type": "application/json"}
    r = requests.post(url, json=body_adf, auth=(user, token), headers=headers, timeout=30)
    if not r.ok:
        logging.error("Jira comment failed: %s %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()


# Main CLI flow
def main():
    parser = argparse.ArgumentParser(description="Build Grafana URL from Jira RIM issue.")
    parser.add_argument("--issue", "-i", default=getenv("ISSUE"), help="Jira issue key (e.g. RIM-319)")
    parser.add_argument("--post", action="store_true", help="Post the constructed Grafana URL to Jira as a comment")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # Load config from env
    JIRA_BASE = getenv("JIRA_BASE")
    JIRA_USER = getenv("JIRA_USER")
    JIRA_TOKEN = getenv("JIRA_TOKEN")
    ISSUE = args.issue
    if not ISSUE:
        logging.error("Issue key required (--issue or set ISSUE env)")
        sys.exit(2)

    PRODUCT_FIELD_ID = getenv("PRODUCT_FIELD_ID", "customfield_11675")
    TIME_FIELD_ID = getenv("TIME_FIELD_ID", "customfield_11609")

    GRAFANA_HOST = getenv("GRAFANA_HOST", "grafana.core.dev.hmnd.ai")
    GRAFANA_UID = getenv("GRAFANA_UID", "13b4a1f2-b2be-47d2-b6de-29951daebc54")
    GRAFANA_SLUG = getenv("GRAFANA_SLUG", "alpha-robot-overview")

    PRE_MS = int(getenv("PRE_MS", "120000"))
    POST_MS = int(getenv("POST_MS", "60000"))

    # Basic checks
    for name, val in [("JIRA_BASE", JIRA_BASE), ("JIRA_USER", JIRA_USER), ("JIRA_TOKEN", JIRA_TOKEN)]:
        if not val:
            logging.error("Environment variable %s is required", name)
            sys.exit(2)

    logging.info("Fetching Jira issue %s...", ISSUE)
    issue_json = jira_get_issue(JIRA_BASE, JIRA_USER, JIRA_TOKEN, ISSUE)

    # Extract product and timing
    fields = issue_json.get("fields", {})
    product_field = fields.get(PRODUCT_FIELD_ID)
    time_field = fields.get(TIME_FIELD_ID)

    product_value = extract_field_value(product_field)
    timing_raw = time_field if time_field else ""

    logging.info("Product: %s", product_value)
    logging.info("Timing raw: %s", timing_raw)

    timing_iso = normalize_jira_time(timing_raw) or ""
    if timing_iso:
        logging.info("Timing ISO: %s", timing_iso)
        incident_ms = iso_to_epoch_ms(timing_iso)
    else:
        # fallback to now
        from time import time
        incident_ms = int(time() * 1000)
        logging.warning("No timing provided; using now: %d", incident_ms)

    from_ms = incident_ms - PRE_MS
    to_ms = incident_ms + POST_MS

    robot_num = extract_robot_num(product_value)
    platform = extract_platform(product_value)

    grafana_url = build_grafana_url(GRAFANA_HOST, GRAFANA_UID, GRAFANA_SLUG,
                                    from_ms, to_ms, platform, robot_num)

    print("\n--- RESULT ---")
    print(f"Issue: {ISSUE}")
    print(f"Product: {product_value}")
    print(f"Platform: {platform}")
    print(f"Robot: {robot_num}")
    print(f"Incident (ms): {incident_ms}")
    print(f"From (ms): {from_ms}")
    print(f"To (ms): {to_ms}")
    print("Grafana URL:")
    print(grafana_url)
    print("--------------\n")

    if args.post:
        comment = (f"RIM snapshot for product *{product_value}* (robot {robot_num}) at {timing_iso or 'unknown'}\n\n"
                   f"Grafana: {grafana_url}")
        logging.info("Posting comment to Jira...")
        res = jira_post_comment(JIRA_BASE, JIRA_USER, JIRA_TOKEN, ISSUE, comment)
        logging.info("Posted comment id: %s", res.get("id"))
        print("Posted comment to Jira.")

if __name__ == "__main__":
    main()