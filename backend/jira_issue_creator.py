import os
import logging
import requests

JIRA_BASE = os.environ.get("JIRA_BASE", "").rstrip("/")
JIRA_USER = os.environ.get("JIRA_USER", "")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
JIRA_DEFAULT_PROJECT = os.environ.get("JIRA_DEFAULT_PROJECT", "RIM")
JIRA_DEFAULT_ISSUE_TYPE = os.environ.get("JIRA_DEFAULT_ISSUE_TYPE", "Robot Issue")
JIRA_FALLBACK_ACCOUNT_ID = os.environ.get("JIRA_FALLBACK_ACCOUNT_ID", "")
AFFECTED_PRODUCT_FIELD_ID = os.environ.get("PRODUCT_FIELD_ID", "customfield_11675")


def get_slack_user_email(user_id: str, slack_client) -> str | None:
    try:
        resp = slack_client.users_info(user=user_id)
        return resp["user"]["profile"].get("email")
    except Exception as e:
        logging.warning("Could not fetch Slack user email for %s: %s", user_id, e)
        return None


def find_jira_account_id(
    email: str,
    jira_base: str | None = None,
    jira_auth: tuple | None = None,
) -> str | None:
    jira_base = (jira_base or JIRA_BASE).rstrip("/")
    jira_auth = jira_auth or (JIRA_USER, JIRA_TOKEN)

    url = f"{jira_base}/rest/api/3/user/search"
    resp = requests.get(
        url,
        params={"query": email},
        auth=jira_auth,
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()

    if not results:
        logging.warning("No Jira account found for email %s", email)
        return None

    for user in results:
        if user.get("emailAddress", "").lower() == email.lower():
            return user["accountId"]

    return results[0]["accountId"]


def resolve_reporter(
    slack_user_id: str,
    slack_client,
    jira_base: str | None = None,
    jira_auth: tuple | None = None,
) -> str:
    email = get_slack_user_email(slack_user_id, slack_client)
    if email:
        account_id = find_jira_account_id(email, jira_base, jira_auth)
        if account_id:
            return account_id
        logging.warning("Slack user %s (%s) has no Jira account — using fallback", slack_user_id, email)
    else:
        logging.warning("Slack user %s has no email — using fallback account", slack_user_id)

    if not JIRA_FALLBACK_ACCOUNT_ID:
        raise ValueError("Cannot map reporter and JIRA_FALLBACK_ACCOUNT_ID is not set")
    return JIRA_FALLBACK_ACCOUNT_ID


def create_issue(
    summary: str,
    slack_permalink: str,
    reporter_account_id: str,
    affected_product_id: str | None = None,
    priority_name: str | None = None,
    project_key: str | None = None,
    issue_type: str | None = None,
    extra_description: str | None = None,
    jira_base: str | None = None,
    jira_auth: tuple | None = None,
) -> str:
    """Returns the new issue key (e.g. 'RIM-1234')."""
    jira_base = (jira_base or JIRA_BASE).rstrip("/")
    jira_auth = jira_auth or (JIRA_USER, JIRA_TOKEN)
    project_key = project_key or JIRA_DEFAULT_PROJECT
    issue_type = issue_type or JIRA_DEFAULT_ISSUE_TYPE

    description_content = [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Created from Slack message: "},
                {
                    "type": "text",
                    "text": slack_permalink,
                    "marks": [{"type": "link", "attrs": {"href": slack_permalink}}],
                },
            ],
        }
    ]

    if extra_description:
        description_content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": extra_description}],
        })

    fields = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": issue_type},
        "reporter": {"id": reporter_account_id},
        "description": {
            "type": "doc",
            "version": 1,
            "content": description_content,
        },
    }
    if affected_product_id:
        fields[AFFECTED_PRODUCT_FIELD_ID] = [{"id": affected_product_id}]
    if priority_name:
        fields["priority"] = {"name": priority_name}

    payload = {"fields": fields}

    url = f"{jira_base}/rest/api/3/issue"
    resp = requests.post(
        url,
        json=payload,
        auth=jira_auth,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    issue_key = resp.json()["key"]
    logging.info("Created Jira issue %s", issue_key)
    return issue_key
