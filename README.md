# Jira MCP Server

A lightweight Jira MCP server for Claude Code, built with [FastMCP](https://github.com/jlowin/fastmcp) and the Jira REST API v3.

## Tools

- **search_issues** - Search issues using JQL
- **read_issue** - Get full details of an issue (description, transitions, etc.)
- **list_projects** - List all accessible projects
- **my_open_issues** - Shortcut for unresolved issues assigned to you
- **add_comment** - Add a comment to an issue
- **transition_issue** - Change issue status (e.g. move to "In Progress", "Done")

## Setup

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### 1. Configure environment

Copy `.env.example` to `.env` and fill in your Jira credentials:

```bash
cp .env.example .env
```

You'll need a [Jira API token](https://id.atlassian.com/manage-profile/security/api-tokens).

### 2. Register with Claude Code

```bash
claude mcp add-json jira '{
  "command": "uv",
  "args": ["run", "/path/to/jira-server/server.py"],
  "env": {
    "JIRA_URL": "https://your-domain.atlassian.net",
    "JIRA_USERNAME": "your-email@example.com",
    "JIRA_API_TOKEN": "your-api-token"
  }
}'
```

Or add it manually to `~/.claude.json` under `mcpServers`.

### 3. Restart Claude Code

The tools will be available as `mcp__jira__*`.

## Dependencies

Managed inline via PEP 723 script metadata — no `requirements.txt` needed. `uv run` handles it automatically.

- `fastmcp>=2.0`
- `httpx`
