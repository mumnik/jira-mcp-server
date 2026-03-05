#!/usr/bin/env python3
"""Lightweight Jira MCP server using the REST API directly."""
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp>=2.0", "httpx"]
# ///

import json
import os

import httpx
from fastmcp import FastMCP

mcp = FastMCP("jira")

JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=JIRA_URL,
        auth=(JIRA_USERNAME, JIRA_API_TOKEN),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )


@mcp.tool()
def search_issues(jql: str, max_results: int = 20) -> str:
    """Search Jira issues using JQL.

    Args:
        jql: JQL query string, e.g. 'assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC'
        max_results: Maximum number of issues to return (default 20, max 100)
    """
    with _client() as client:
        resp = client.post(
            "/rest/api/3/search/jql",
            json={
                "jql": jql,
                "fields": ["summary", "status", "priority", "issuetype", "assignee", "reporter", "updated", "created", "description"],
                "maxResults": min(max_results, 100),
            },
        )
        resp.raise_for_status()
        data = resp.json()

    issues = []
    for raw in data.get("issues", []):
        f = raw.get("fields", {})
        issue = {
            "key": raw["key"],
            "summary": f.get("summary"),
            "status": f.get("status", {}).get("name"),
            "priority": f.get("priority", {}).get("name"),
            "type": f.get("issuetype", {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "reporter": (f.get("reporter") or {}).get("displayName"),
            "updated": f.get("updated"),
            "created": f.get("created"),
        }
        issues.append(issue)

    return json.dumps({"total": len(issues), "issues": issues}, indent=2)


@mcp.tool()
def read_issue(issue_key: str) -> str:
    """Get details of a specific Jira issue.

    Args:
        issue_key: The issue key, e.g. 'TSP-123'
    """
    with _client() as client:
        resp = client.get(
            f"/rest/api/3/issue/{issue_key}",
            params={"expand": "transitions"},
        )
        resp.raise_for_status()
        data = resp.json()

    f = data.get("fields", {})

    # Extract description text from ADF
    desc = ""
    desc_doc = f.get("description")
    if desc_doc and isinstance(desc_doc, dict):
        for block in desc_doc.get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    desc += inline.get("text", "")
            desc += "\n"

    transitions = [
        {"id": t["id"], "name": t["name"]}
        for t in data.get("transitions", [])
    ]

    result = {
        "key": data["key"],
        "summary": f.get("summary"),
        "description": desc.strip(),
        "status": f.get("status", {}).get("name"),
        "priority": f.get("priority", {}).get("name"),
        "type": f.get("issuetype", {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "reporter": (f.get("reporter") or {}).get("displayName"),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "labels": f.get("labels", []),
        "transitions": transitions,
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def list_projects() -> str:
    """List all Jira projects you have access to."""
    with _client() as client:
        resp = client.get("/rest/api/3/project/search", params={"maxResults": 50})
        resp.raise_for_status()
        data = resp.json()

    projects = []
    for p in data.get("values", []):
        projects.append({
            "key": p["key"],
            "name": p["name"],
            "type": p.get("projectTypeKey"),
            "lead": (p.get("lead") or {}).get("displayName"),
        })
    return json.dumps(projects, indent=2)


@mcp.tool()
def my_open_issues(max_results: int = 30) -> str:
    """Get all unresolved issues assigned to you, sorted by update time.

    Args:
        max_results: Maximum number of issues to return (default 30)
    """
    return search_issues(
        jql="assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
        max_results=max_results,
    )


@mcp.tool()
def add_comment(issue_key: str, comment: str) -> str:
    """Add a comment to a Jira issue.

    Args:
        issue_key: The issue key, e.g. 'TSP-123'
        comment: The comment text to add
    """
    body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
        }
    }
    with _client() as client:
        resp = client.post(f"/rest/api/3/issue/{issue_key}/comment", json=body)
        resp.raise_for_status()
        data = resp.json()

    return json.dumps({"id": data["id"], "created": data["created"]}, indent=2)


@mcp.tool()
def transition_issue(issue_key: str, transition_name: str) -> str:
    """Change the status of a Jira issue (e.g. move to 'In Progress', 'Done').

    Args:
        issue_key: The issue key, e.g. 'TSP-123'
        transition_name: The target status name (use read_issue to see available transitions)
    """
    with _client() as client:
        # Get available transitions
        resp = client.get(f"/rest/api/3/issue/{issue_key}/transitions")
        resp.raise_for_status()
        transitions = resp.json().get("transitions", [])

        match = None
        for t in transitions:
            if t["name"].lower() == transition_name.lower():
                match = t
                break

        if not match:
            available = [t["name"] for t in transitions]
            return json.dumps({"error": f"Transition '{transition_name}' not found. Available: {available}"})

        resp = client.post(
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        resp.raise_for_status()

    return json.dumps({"success": True, "issue": issue_key, "new_status": transition_name})


if __name__ == "__main__":
    mcp.run()
