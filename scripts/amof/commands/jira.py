"""Jira command - pull ticket context for AI agents."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
import base64
from pathlib import Path
from typing import Any, Dict, Optional


def get_atlassian_auth() -> tuple[str, str] | None:
    """Get Atlassian credentials from environment."""
    user = os.environ.get("ATLASSIAN_USER")
    token = os.environ.get("ATLASSIAN_TOKEN")
    if not user or not token:
        return None
    return (user, token)


def get_jira_url() -> str:
    """Get Jira base URL from environment."""
    return os.environ.get("ATLASSIAN_URL", "https://jira.example.com")


def fetch_jira_issue(issue_key: str) -> dict | None:
    """Fetch issue details from Jira REST API."""
    auth = get_atlassian_auth()
    if not auth:
        sys.stderr.write("[jira] Atlassian credentials not configured\n")
        sys.stderr.write("[jira] Set ATLASSIAN_USER and ATLASSIAN_TOKEN in .env\n")
        return None
    
    base_url = get_jira_url()
    api_url = f"{base_url}/rest/api/2/issue/{issue_key}"
    
    try:
        credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
        }
        
        req = urllib.request.Request(api_url, headers=headers, method="GET")
        
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.stderr.write(f"[jira] Issue not found: {issue_key}\n")
        elif e.code == 401:
            sys.stderr.write("[jira] Authentication failed. Check credentials.\n")
        elif e.code == 403:
            sys.stderr.write("[jira] Permission denied. Check your access.\n")
        else:
            sys.stderr.write(f"[jira] HTTP {e.code}: {e.reason}\n")
        return None
    
    except urllib.error.URLError as e:
        sys.stderr.write(f"[jira] Connection error: {e.reason}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"[jira] Error: {e}\n")
        return None


def format_issue_info(issue: dict) -> str:
    """Format issue data for display."""
    fields = issue.get("fields", {})
    
    lines = []
    lines.append(f"Key: {issue.get('key')}")
    lines.append(f"Summary: {fields.get('summary', 'N/A')}")
    lines.append(f"Status: {fields.get('status', {}).get('name', 'N/A')}")
    lines.append(f"Type: {fields.get('issuetype', {}).get('name', 'N/A')}")
    lines.append(f"Priority: {fields.get('priority', {}).get('name', 'N/A')}")
    
    assignee = fields.get("assignee")
    if assignee:
        lines.append(f"Assignee: {assignee.get('displayName', assignee.get('name', 'N/A'))}")
    else:
        lines.append("Assignee: Unassigned")
    
    reporter = fields.get("reporter")
    if reporter:
        lines.append(f"Reporter: {reporter.get('displayName', reporter.get('name', 'N/A'))}")
    
    created = fields.get("created", "")[:10]
    updated = fields.get("updated", "")[:10]
    if created:
        lines.append(f"Created: {created}")
    if updated:
        lines.append(f"Updated: {updated}")
    
    # Components
    components = fields.get("components", [])
    if components:
        comp_names = [c.get("name") for c in components]
        lines.append(f"Components: {', '.join(comp_names)}")
    
    # Labels
    labels = fields.get("labels", [])
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    
    # Description
    description = fields.get("description", "")
    if description:
        lines.append("")
        lines.append("Description:")
        lines.append("-" * 40)
        # Truncate long descriptions
        if len(description) > 2000:
            description = description[:2000] + "\n... (truncated)"
        lines.append(description)
    
    return "\n".join(lines)


def format_issue_context(issue: dict) -> str:
    """Format issue as markdown context for AI agents."""
    fields = issue.get("fields", {})
    key = issue.get("key", "UNKNOWN")
    
    lines = []
    lines.append(f"# {key}: {fields.get('summary', 'No summary')}")
    lines.append("")
    
    # Metadata table
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Status | {fields.get('status', {}).get('name', 'N/A')} |")
    lines.append(f"| Type | {fields.get('issuetype', {}).get('name', 'N/A')} |")
    lines.append(f"| Priority | {fields.get('priority', {}).get('name', 'N/A')} |")
    
    assignee = fields.get("assignee")
    if assignee:
        lines.append(f"| Assignee | {assignee.get('displayName', 'N/A')} |")
    
    components = fields.get("components", [])
    if components:
        comp_names = [c.get("name") for c in components]
        lines.append(f"| Components | {', '.join(comp_names)} |")
    
    labels = fields.get("labels", [])
    if labels:
        lines.append(f"| Labels | {', '.join(labels)} |")
    
    lines.append("")
    
    # Description
    description = fields.get("description", "")
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")
    
    # Acceptance criteria (custom field - may vary)
    # Common field names for acceptance criteria
    for field_name in ["customfield_10200", "customfield_10001", "acceptance_criteria"]:
        ac = fields.get(field_name)
        if ac:
            lines.append("## Acceptance Criteria")
            lines.append("")
            lines.append(ac)
            lines.append("")
            break
    
    # Linked issues
    issue_links = fields.get("issuelinks", [])
    if issue_links:
        lines.append("## Related Issues")
        lines.append("")
        for link in issue_links:
            link_type = link.get("type", {}).get("name", "relates to")
            if "outwardIssue" in link:
                linked = link["outwardIssue"]
                lines.append(f"- {link_type}: {linked.get('key')} - {linked.get('fields', {}).get('summary', '')}")
            if "inwardIssue" in link:
                linked = link["inwardIssue"]
                lines.append(f"- {link_type}: {linked.get('key')} - {linked.get('fields', {}).get('summary', '')}")
        lines.append("")
    
    # Subtasks
    subtasks = fields.get("subtasks", [])
    if subtasks:
        lines.append("## Subtasks")
        lines.append("")
        for st in subtasks:
            status = st.get("fields", {}).get("status", {}).get("name", "")
            done = "✓" if status.lower() in ["done", "closed", "resolved"] else "○"
            lines.append(f"- [{done}] {st.get('key')}: {st.get('fields', {}).get('summary', '')}")
        lines.append("")
    
    return "\n".join(lines)


def cmd_jira_info(ticket_id: str) -> int:
    """Show ticket details."""
    print(f"[jira] Fetching {ticket_id}...")
    
    issue = fetch_jira_issue(ticket_id)
    if not issue:
        return 1
    
    print()
    print(format_issue_info(issue))
    return 0


def cmd_jira_context(ticket_id: str, output_path: Optional[str] = None) -> int:
    """Generate AI context from ticket."""
    print(f"[jira] Fetching {ticket_id}...")
    
    issue = fetch_jira_issue(ticket_id)
    if not issue:
        return 1
    
    context = format_issue_context(issue)
    
    if output_path:
        output_file = Path(output_path)
    else:
        # Default to context/_ticket/<ticket>.md
        output_dir = Path("context/_ticket")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{ticket_id}.md"
    
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(context, encoding="utf-8")
    
    print(f"[jira] Saved context to {output_file}")
    return 0


def cmd_jira(args, manifest: Dict[str, Any]) -> int:
    """Handle jira subcommands."""
    jira_cmd = getattr(args, "jira_cmd", None)
    
    if jira_cmd == "info":
        return cmd_jira_info(args.ticket)
    elif jira_cmd == "context":
        output = getattr(args, "output", None)
        return cmd_jira_context(args.ticket, output)
    else:
        sys.stderr.write("Usage: amof jira <info|context> <ticket>\n")
        return 1

