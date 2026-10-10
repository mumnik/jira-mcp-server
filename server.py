#!/usr/bin/env python3
"""Lightweight Jira MCP server using the REST API directly."""
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp>=2.0", "httpx"]
# ///

import json
import os
import re
from datetime import date as _date

import httpx
from fastmcp import FastMCP

mcp = FastMCP("jira")

JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# Everything written into Jira is English: issues and comments are read by
# people who do not speak Russian, and a Russian description has to be found
# and rewritten by hand later. Asking the author to remember that has failed
# repeatedly, so the rule is enforced here instead — a rejected tool call costs
# one retry, cleaning up after the fact costs someone's afternoon.
#
# The test is a ratio rather than "any Cyrillic at all" on purpose. A quoted log
# line, a Latvian or Russian UI string, a customer's own words in a bug report —
# those are legitimate content and should pass. A description written in Russian
# is nowhere near the threshold.
CYRILLIC_TOLERANCE = float(os.environ.get("JIRA_CYRILLIC_TOLERANCE", "0.10"))

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def _require_english(field: str, text: str | None, allow_cyrillic: bool = False) -> None:
    """Raise unless `text` is predominantly non-Cyrillic.

    Raises ValueError, which FastMCP turns into a tool error — the write never
    reaches Jira, so nothing has to be cleaned up afterwards.
    """
    if allow_cyrillic or not text:
        return

    letters = _LETTER_RE.findall(text)
    if not letters:
        return

    cyrillic = sum(1 for ch in letters if _CYRILLIC_RE.match(ch))
    ratio = cyrillic / len(letters)
    if ratio <= CYRILLIC_TOLERANCE:
        return

    offending = next(
        (line.strip() for line in text.splitlines() if _CYRILLIC_RE.search(line)),
        text.strip(),
    )
    if len(offending) > 120:
        offending = offending[:117] + "..."

    raise ValueError(
        f"{field} is {ratio:.0%} Cyrillic — Jira content must be written in English. "
        f"First offending line: {offending!r}. "
        "Rewrite the text in English and call again. If the Cyrillic is quoted "
        "content that must be preserved verbatim (a log line, a UI string, a "
        "customer's own words), pass allow_cyrillic=True."
    )


def _adf_paragraphs(text: str | None) -> list[dict]:
    """Turn plain text into ADF paragraph blocks, one per line.

    Blank lines become empty paragraphs so the spacing of the source text
    survives the round trip.
    """
    blocks: list[dict] = []
    for paragraph in (text or "").split("\n"):
        if paragraph.strip():
            blocks.append(
                {"type": "paragraph", "content": [{"type": "text", "text": paragraph}]}
            )
        else:
            blocks.append({"type": "paragraph", "content": []})
    return blocks


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
def create_issue(
    project_key: str,
    summary: str,
    issue_type: str = "Story",
    description: str = "",
    labels: list[str] | None = None,
    assignee_name: str | None = None,
    parent_key: str | None = None,
    allow_cyrillic: bool = False,
) -> str:
    """Create a new Jira issue. Summary and description MUST be in English.

    Args:
        project_key: The project key, e.g. 'TSP'
        summary: Issue title/summary. English only.
        issue_type: Issue type name (default: 'Story'). Common values: Story, Bug, Task, Sub-task
        description: Issue description in plain text (supports multiple paragraphs separated by newlines). English only.
        labels: Optional list of labels to apply
        assignee_name: Optional display name of the assignee (will look up account ID)
        parent_key: Parent issue key (e.g. 'TSP-619'). Required when issue_type is 'Sub-task';
            also usable to nest standard issue types under a parent where the project allows it.
        allow_cyrillic: Escape hatch for text that is legitimately Cyrillic — a quoted log
            line, a UI string, a customer's own words. Do not set it to write Russian prose.
    """
    _require_english("summary", summary, allow_cyrillic)
    _require_english("description", description, allow_cyrillic)

    content_blocks = _adf_paragraphs(description)

    fields: dict = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": issue_type},
    }

    if content_blocks:
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": content_blocks,
        }

    if labels:
        fields["labels"] = labels

    if parent_key:
        fields["parent"] = {"key": parent_key}

    if assignee_name:
        # Look up account ID by display name
        with _client() as client:
            resp = client.get(
                "/rest/api/3/user/search",
                params={"query": assignee_name, "maxResults": 1},
            )
            resp.raise_for_status()
            users = resp.json()
            if users:
                fields["assignee"] = {"accountId": users[0]["accountId"]}

    with _client() as client:
        resp = client.post("/rest/api/3/issue", json={"fields": fields})
        if resp.status_code >= 400:
            return json.dumps({"error": resp.status_code, "body": resp.text}, indent=2)
        data = resp.json()

    return json.dumps({
        "key": data["key"],
        "id": data["id"],
        "url": f"{JIRA_URL}/browse/{data['key']}",
    }, indent=2)


@mcp.tool()
def update_issue(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
    allow_cyrillic: bool = False,
) -> str:
    """Edit an existing issue's summary, description or labels. English only.

    Only the fields passed are touched; omitted ones keep their current value.
    Note that description is REPLACED, not appended — read the issue first if
    the existing text has to be preserved.

    Args:
        issue_key: The issue key, e.g. 'TSP-123'
        summary: New title. English only. Omit to keep the current one.
        description: New description in plain text, paragraphs separated by
            newlines. English only. Omit to keep the current one.
        labels: Replacement list of labels. Omit to keep the current ones.
        allow_cyrillic: Escape hatch for text that is legitimately Cyrillic — a quoted log
            line, a UI string, a customer's own words. Do not set it to write Russian prose.
    """
    _require_english("summary", summary, allow_cyrillic)
    _require_english("description", description, allow_cyrillic)

    fields: dict = {}

    if summary is not None:
        fields["summary"] = summary

    if description is not None:
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": _adf_paragraphs(description),
        }

    if labels is not None:
        fields["labels"] = labels

    if not fields:
        raise ValueError(
            "Nothing to update: pass at least one of summary, description or labels."
        )

    with _client() as client:
        resp = client.put(f"/rest/api/3/issue/{issue_key}", json={"fields": fields})
        if resp.status_code >= 400:
            return json.dumps({"error": resp.status_code, "body": resp.text}, indent=2)

    return json.dumps({
        "key": issue_key,
        "updated": sorted(fields),
        "url": f"{JIRA_URL}/browse/{issue_key}",
    }, indent=2)


@mcp.tool()
def add_comment(issue_key: str, comment: str, allow_cyrillic: bool = False) -> str:
    """Add a comment to a Jira issue. The comment MUST be in English.

    Args:
        issue_key: The issue key, e.g. 'TSP-123'
        comment: The comment text to add. English only.
        allow_cyrillic: Escape hatch for text that is legitimately Cyrillic — a quoted log
            line, a UI string, a customer's own words. Do not set it to write Russian prose.
    """
    _require_english("comment", comment, allow_cyrillic)

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


# ─────────────────────────── Confluence ───────────────────────────
# Confluence Cloud lives on the same Atlassian site under /wiki and uses the
# same credentials as Jira, so it reuses _client() / JIRA_URL.

CONFLUENCE_BASE = "/wiki"


def _conf_space_id(client: httpx.Client, space_key: str) -> str:
    resp = client.get(
        f"{CONFLUENCE_BASE}/api/v2/spaces",
        params={"keys": space_key, "limit": 1},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"Confluence space '{space_key}' not found")
    return results[0]["id"]


def _conf_page_url(data: dict) -> str:
    links = data.get("_links", {}) or {}
    webui = links.get("webui")
    if webui:
        base = links.get("base") or f"{JIRA_URL}{CONFLUENCE_BASE}"
        return f"{base}{webui}"
    return f"{JIRA_URL}{CONFLUENCE_BASE}/pages/{data.get('id')}"


@mcp.tool()
def confluence_search(cql: str, max_results: int = 20) -> str:
    """Search Confluence content using CQL.

    Args:
        cql: CQL query, e.g. 'space = TS AND type = page AND title ~ "passbook"'
        max_results: Maximum number of results (default 20, max 100)
    """
    with _client() as client:
        resp = client.get(
            f"{CONFLUENCE_BASE}/rest/api/content/search",
            params={"cql": cql, "limit": min(max_results, 100)},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for r in data.get("results", []):
        webui = (r.get("_links") or {}).get("webui", "")
        results.append({
            "id": r.get("id"),
            "type": r.get("type"),
            "title": r.get("title"),
            "url": f"{JIRA_URL}{CONFLUENCE_BASE}{webui}" if webui else None,
        })
    return json.dumps({"total": len(results), "results": results}, indent=2)


@mcp.tool()
def confluence_get_page(page_id: str) -> str:
    """Read a Confluence page's title and body (storage format).

    Args:
        page_id: The page id
    """
    with _client() as client:
        resp = client.get(
            f"{CONFLUENCE_BASE}/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        resp.raise_for_status()
        data = resp.json()

    return json.dumps({
        "id": data["id"],
        "title": data.get("title"),
        "version": (data.get("version") or {}).get("number"),
        "body": ((data.get("body") or {}).get("storage") or {}).get("value", ""),
        "url": _conf_page_url(data),
    }, indent=2)


@mcp.tool()
def confluence_create_page(
    space_key: str,
    title: str,
    body: str,
    parent_id: str | None = None,
    representation: str = "storage",
    allow_cyrillic: bool = False,
) -> str:
    """Create a Confluence page. Title and body MUST be in English.

    Args:
        space_key: Space key, e.g. 'TS'
        title: Page title (must be unique within the space)
        body: Page body. For representation='storage' pass Confluence storage-format
            XHTML, e.g. '<h1>Heading</h1><p>Text</p><ul><li>item</li></ul>'.
            For representation='wiki' pass wiki markup.
        parent_id: Optional parent page id to nest the new page under
        representation: Body format: 'storage' (default, XHTML) or 'wiki'
        allow_cyrillic: Escape hatch for text that is legitimately Cyrillic — a quoted log
            line, a UI string, a customer's own words. Do not set it to write Russian prose.
    """
    _require_english("title", title, allow_cyrillic)
    _require_english("body", body, allow_cyrillic)

    with _client() as client:
        space_id = _conf_space_id(client, space_key)
        payload: dict = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": representation, "value": body},
        }
        if parent_id:
            payload["parentId"] = parent_id

        resp = client.post(f"{CONFLUENCE_BASE}/api/v2/pages", json=payload)
        if resp.status_code >= 400:
            return json.dumps({"error": resp.status_code, "body": resp.text}, indent=2)
        data = resp.json()

    return json.dumps({
        "id": data["id"],
        "title": data.get("title"),
        "url": _conf_page_url(data),
    }, indent=2)


@mcp.tool()
def confluence_update_page(
    page_id: str,
    body: str,
    title: str | None = None,
    representation: str = "storage",
    version_message: str = "",
    allow_cyrillic: bool = False,
) -> str:
    """Update an existing Confluence page (replaces its body). Content MUST be in English.

    Args:
        page_id: The page id
        body: New body content (see confluence_create_page for the representation format)
        title: New title (optional; keeps the current title if omitted)
        representation: Body format: 'storage' (default) or 'wiki'
        version_message: Optional change note stored with the new version
        allow_cyrillic: Escape hatch for text that is legitimately Cyrillic — a quoted log
            line, a UI string, a customer's own words. Do not set it to write Russian prose.
    """
    _require_english("title", title, allow_cyrillic)
    _require_english("body", body, allow_cyrillic)
    _require_english("version_message", version_message, allow_cyrillic)

    with _client() as client:
        resp = client.get(f"{CONFLUENCE_BASE}/api/v2/pages/{page_id}")
        resp.raise_for_status()
        current = resp.json()
        next_version = (current.get("version") or {}).get("number", 1) + 1

        payload = {
            "id": page_id,
            "status": "current",
            "title": title or current.get("title"),
            "body": {"representation": representation, "value": body},
            "version": {"number": next_version, "message": version_message},
        }
        resp = client.put(f"{CONFLUENCE_BASE}/api/v2/pages/{page_id}", json=payload)
        if resp.status_code >= 400:
            return json.dumps({"error": resp.status_code, "body": resp.text}, indent=2)
        data = resp.json()

    return json.dumps({
        "id": data["id"],
        "title": data.get("title"),
        "version": next_version,
        "url": _conf_page_url(data),
    }, indent=2)


# ─────────────────────────── Releases / Versions ───────────────────────────
# A Jira "release" is a project Version. The lifecycle is: create a version →
# tag issues with it as their fixVersion → mark it released (with a date) →
# read back the issues in it as release notes. Company-managed and team-managed
# projects both expose versions through /rest/api/3, so these reuse _client().


def _project_id(client: httpx.Client, project_key: str) -> int:
    resp = client.get(f"/rest/api/3/project/{project_key}")
    resp.raise_for_status()
    return int(resp.json()["id"])


def _jql_literal(value: str) -> str:
    """Quote a value for safe interpolation into JQL."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _version_url(project_key: str, version_id: str | int) -> str:
    return f"{JIRA_URL}/projects/{project_key}/versions/{version_id}"


def _dated_version_name(existing: set[str], day: str) -> str:
    """Pick the next version name for a day under the ticketshop convention.

    Names are the release day as ddmmyyyy. When more than one release happens on
    the same day the later ones get a -2, -3, ... suffix. The first free name in
    that sequence is returned, so the Nth release of a day lands on <day>-N.
    """
    if day not in existing:
        return day
    n = 2
    while f"{day}-{n}" in existing:
        n += 1
    return f"{day}-{n}"


@mcp.tool()
def list_versions(
    project_key: str,
    include_released: bool = True,
    include_archived: bool = False,
    max_results: int = 50,
) -> str:
    """List the releases (versions) of a project.

    Use this to find a version's id before updating or releasing it, and to see
    what releases already exist.

    Args:
        project_key: The project key, e.g. 'TSP'
        include_released: Include versions already marked released (default True)
        include_archived: Include archived versions (default False)
        max_results: Cap on versions returned (default 50)
    """
    with _client() as client:
        resp = client.get(f"/rest/api/3/project/{project_key}/versions")
        resp.raise_for_status()
        data = resp.json()

    versions = []
    for v in data:
        if not include_released and v.get("released"):
            continue
        if not include_archived and v.get("archived"):
            continue
        versions.append({
            "id": v.get("id"),
            "name": v.get("name"),
            "released": v.get("released", False),
            "archived": v.get("archived", False),
            "startDate": v.get("startDate"),
            "releaseDate": v.get("releaseDate"),
            "overdue": v.get("overdue", False),
            "description": v.get("description"),
        })
    return json.dumps({"total": len(versions), "versions": versions[:max_results]}, indent=2)


@mcp.tool()
def create_version(
    project_key: str,
    name: str | None = None,
    description: str = "",
    start_date: str | None = None,
    release_date: str | None = None,
    released: bool = False,
    date: str | None = None,
    allow_cyrillic: bool = False,
) -> str:
    """Create a release (version) in a project. Name and description MUST be in English.

    If `name` is omitted the name is generated under the ticketshop convention:
    the release day as ddmmyyyy (e.g. '10102026'), and if a version for that day
    already exists the next one becomes '10102026-2', '10102026-3', and so on.
    The day used is `date`, else `release_date`, else today.

    Args:
        project_key: The project key, e.g. 'TSP'
        name: Version name. Omit it to auto-generate a dated name (see above).
            Pass an explicit name only for off-convention versions. English only.
        description: Optional description. English only.
        start_date: Optional start date as 'YYYY-MM-DD'
        release_date: Optional release date as 'YYYY-MM-DD'
        released: Create it already marked released (default False — usually you
            create it open, tag issues, then release it with update_version)
        date: Day for the auto-generated name as 'YYYY-MM-DD' (default today).
            Ignored when `name` is given.
        allow_cyrillic: Escape hatch for legitimately Cyrillic text (a quoted log
            line, a UI string). Do not set it to write Russian prose.
    """
    _require_english("name", name, allow_cyrillic)
    _require_english("description", description, allow_cyrillic)

    with _client() as client:
        project_id = _project_id(client, project_key)

        if not name:
            day_src = date or release_date
            if day_src:
                try:
                    day = _date.fromisoformat(day_src).strftime("%d%m%Y")
                except ValueError:
                    return json.dumps(
                        {"error": "bad_date", "detail": f"expected YYYY-MM-DD, got {day_src!r}"},
                        indent=2,
                    )
            else:
                day = _date.today().strftime("%d%m%Y")
            vresp = client.get(f"/rest/api/3/project/{project_key}/versions")
            vresp.raise_for_status()
            existing = {v.get("name") for v in vresp.json()}
            name = _dated_version_name(existing, day)

        payload: dict = {
            "name": name,
            "projectId": project_id,
            "released": released,
        }
        if description:
            payload["description"] = description
        if start_date:
            payload["startDate"] = start_date
        if release_date:
            payload["releaseDate"] = release_date

        resp = client.post("/rest/api/3/version", json=payload)
        if resp.status_code >= 400:
            return json.dumps({"error": resp.status_code, "body": resp.text}, indent=2)
        data = resp.json()

    return json.dumps({
        "id": data["id"],
        "name": data.get("name"),
        "released": data.get("released", False),
        "releaseDate": data.get("releaseDate"),
        "url": _version_url(project_key, data["id"]),
    }, indent=2)


@mcp.tool()
def update_version(
    version_id: str,
    name: str | None = None,
    description: str | None = None,
    start_date: str | None = None,
    release_date: str | None = None,
    released: bool | None = None,
    archived: bool | None = None,
    allow_cyrillic: bool = False,
) -> str:
    """Edit a release (version): rename, set dates, release it, or archive it.

    Only the fields passed are changed. To RELEASE a version, call with
    released=True (and usually release_date='YYYY-MM-DD'). To re-open a mistaken
    release, released=False. Get the version_id from list_versions.

    Args:
        version_id: The version id (numeric string), from list_versions
        name: New name. English only. Omit to keep current.
        description: New description. English only. Omit to keep current.
        start_date: Start date 'YYYY-MM-DD', or '' to clear. Omit to keep current.
        release_date: Release date 'YYYY-MM-DD', or '' to clear. Omit to keep current.
        released: Mark released (True) / unreleased (False). Omit to keep current.
        archived: Archive (True) / unarchive (False). Omit to keep current.
        allow_cyrillic: Escape hatch for legitimately Cyrillic text.
    """
    _require_english("name", name, allow_cyrillic)
    _require_english("description", description, allow_cyrillic)

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if start_date is not None:
        payload["startDate"] = start_date
    if release_date is not None:
        payload["releaseDate"] = release_date
    if released is not None:
        payload["released"] = released
    if archived is not None:
        payload["archived"] = archived

    if not payload:
        raise ValueError(
            "Nothing to update: pass at least one of name, description, "
            "start_date, release_date, released or archived."
        )

    with _client() as client:
        resp = client.put(f"/rest/api/3/version/{version_id}", json=payload)
        if resp.status_code >= 400:
            return json.dumps({"error": resp.status_code, "body": resp.text}, indent=2)
        data = resp.json()

    return json.dumps({
        "id": data.get("id", version_id),
        "name": data.get("name"),
        "released": data.get("released"),
        "archived": data.get("archived"),
        "releaseDate": data.get("releaseDate"),
        "changed": sorted(payload),
    }, indent=2)


@mcp.tool()
def set_issue_fix_version(
    issue_keys: list[str],
    version_name: str,
    action: str = "add",
) -> str:
    """Tag issues with a release (fixVersion), or untag them.

    Args:
        issue_keys: One or more issue keys, e.g. ['TSP-123', 'TSP-124']
        version_name: The version name exactly as it appears in the project
            (must already exist — create it with create_version first)
        action: 'add' (default) to add the version alongside any existing ones,
            'remove' to drop it, or 'set' to replace ALL fixVersions with just this one.
    """
    if action not in ("add", "remove", "set"):
        raise ValueError("action must be 'add', 'remove' or 'set'")

    if action == "set":
        body = {"fields": {"fixVersions": [{"name": version_name}]}}
    else:
        body = {"update": {"fixVersions": [{action: {"name": version_name}}]}}

    results = []
    with _client() as client:
        for key in issue_keys:
            resp = client.put(f"/rest/api/3/issue/{key}", json=body)
            if resp.status_code >= 400:
                results.append({"issue": key, "ok": False, "error": resp.status_code, "body": resp.text})
            else:
                results.append({"issue": key, "ok": True, "action": action, "version": version_name})

    return json.dumps({"total": len(results), "results": results}, indent=2)


@mcp.tool()
def release_notes(
    project_key: str | None = None,
    version_name: str | None = None,
    jql: str | None = None,
    group_by: str = "type",
    max_results: int = 200,
) -> str:
    """Assemble Markdown release notes from the issues in a release.

    Collects the issues (by project + version, or by a custom JQL), groups them,
    and returns formatted Markdown ready to paste into a changelog, a Jira
    version description, or a Confluence page (via confluence_create_page).
    This only READS Jira and formats the result — it writes nothing.

    Args:
        project_key: Project key, e.g. 'TSP' (required unless jql is given)
        version_name: Version/release name (required unless jql is given)
        jql: Custom JQL selecting the issues instead of project+version, e.g.
            'project = TSP AND fixVersion = "core-1.0.75" AND statusCategory = Done'
        group_by: How to group the sections: 'type' (issue type, default),
            'status', or 'component'
        max_results: Cap on issues scanned (default 200, max 100 per API page —
            paginated internally)
    """
    if group_by not in ("type", "status", "component"):
        raise ValueError("group_by must be 'type', 'status' or 'component'")

    if jql:
        query = jql
    elif project_key and version_name:
        query = (
            f"project = {project_key} AND fixVersion = {_jql_literal(version_name)} "
            "ORDER BY issuetype ASC, key ASC"
        )
    else:
        raise ValueError("Pass either jql, or both project_key and version_name.")

    # Fetch issues (paginate so release_notes isn't capped at one API page).
    issues: list[dict] = []
    with _client() as client:
        next_token = None
        while len(issues) < max_results:
            payload = {
                "jql": query,
                "fields": ["summary", "status", "issuetype", "components", "resolution"],
                "maxResults": min(100, max_results - len(issues)),
            }
            if next_token:
                payload["nextPageToken"] = next_token
            resp = client.post("/rest/api/3/search/jql", json=payload)
            resp.raise_for_status()
            data = resp.json()
            issues.extend(data.get("issues", []))
            next_token = data.get("nextPageToken")
            if not next_token or not data.get("issues"):
                break

        # Best-effort release metadata for the header.
        released_on = None
        if project_key and version_name:
            vresp = client.get(f"/rest/api/3/project/{project_key}/versions")
            if vresp.status_code < 400:
                for v in vresp.json():
                    if v.get("name") == version_name:
                        released_on = v.get("releaseDate")
                        break

    # Group.
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for raw in issues:
        f = raw.get("fields", {})
        key = raw["key"]
        summary = f.get("summary") or ""
        status = (f.get("status") or {}).get("name") or "No status"
        if group_by == "type":
            buckets = [(f.get("issuetype") or {}).get("name") or "Other"]
        elif group_by == "status":
            buckets = [status]
        else:  # component
            comps = [c.get("name") for c in (f.get("components") or []) if c.get("name")]
            buckets = comps or ["Uncategorised"]
        for b in buckets:
            groups.setdefault(b, []).append((key, summary, status))

    # Render Markdown.
    title = (
        f"{project_key} — {version_name}" if (project_key and version_name)
        else "Release notes"
    )
    lines = [f"# {title} — Release notes"]
    if released_on:
        lines.append(f"\n_Released: {released_on}_")
    lines.append(f"\n_{len(issues)} issue(s)_\n")

    for bucket in sorted(groups):
        lines.append(f"## {bucket}")
        for key, summary, status in groups[bucket]:
            lines.append(f"- **{key}** {summary} _({status})_")
        lines.append("")

    if not issues:
        lines.append("_No issues found for this release._")

    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    mcp.run()
