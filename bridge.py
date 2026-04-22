#!/usr/bin/env python3
"""Moltbook Memory Bridge — Railway worker service.

Lets Moltbook agents use Agent Memory via !memory commands in comments/DMs.
Runs as a long-lived worker, polling every 2 minutes. Zero VPS dependency.

Stateless design: checks if we already replied before responding,
so restarts don't cause duplicate responses.

Talks to Agent Memory through the public MCP endpoint (SSE) — same
access level as any other agent. No service keys, no admin access.

Environment variables:
    MOLTBOOK_API_KEY     — Moltbook API key for systemadmin_sylex
    AGENT_MEMORY_URL     — Agent Memory MCP endpoint (default: production)
    POLL_INTERVAL        — Seconds between polls (default: 120)
    MAX_MESSAGE_AGE      — Ignore messages older than this many seconds (default: 600)
    MAX_RESPONSES_PER_RUN — Cap responses per poll cycle (default: 5)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone


# --- Configuration ---

MOLTBOOK_API = "https://moltbook.com/api/v1"
MOLTBOOK_KEY = os.environ.get("MOLTBOOK_API_KEY", "")
MOLTBOOK_USERNAME = "systemadmin_sylex"

AGENT_MEMORY_URL = os.environ.get(
    "AGENT_MEMORY_URL",
    "https://agent-memory-production-6506.up.railway.app",
).rstrip("/")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
MAX_MESSAGE_AGE = int(os.environ.get("MAX_MESSAGE_AGE", "600"))
MAX_RESPONSES_PER_RUN = int(os.environ.get("MAX_RESPONSES_PER_RUN", "5"))


# --- Logging ---

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- Agent Memory MCP client ---

def call_agent_memory(tool_name: str, arguments: dict, timeout: float = 20.0) -> str:
    """Call an Agent Memory tool via the MCP SSE endpoint.

    Handles the full MCP lifecycle: SSE connect -> initialize -> tool call -> result.
    Returns the text result or an error string.
    """
    result = {"error": None, "data": None}
    session_id = None
    done = threading.Event()

    def send_message(payload: dict):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{AGENT_MEMORY_URL}/messages/?session_id={session_id}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)

    def listen_sse():
        nonlocal session_id
        try:
            req = urllib.request.Request(f"{AGENT_MEMORY_URL}/sse")
            resp = urllib.request.urlopen(req, timeout=timeout)
            initialized = False
            for raw_line in resp:
                line = raw_line.decode().strip()
                if "session_id=" in line and line.startswith("data:"):
                    path = line.split("data:", 1)[1].strip()
                    session_id = path.split("session_id=")[1]
                    send_message({
                        "jsonrpc": "2.0", "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "moltbook-bridge", "version": "1.0.0"},
                        },
                    })
                elif line.startswith("data:") and "jsonrpc" in line:
                    data = line.split("data:", 1)[1].strip()
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    msg_id = parsed.get("id")
                    if msg_id == 0 and not initialized:
                        initialized = True
                        send_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
                        send_message({
                            "jsonrpc": "2.0", "id": 1,
                            "method": "tools/call",
                            "params": {"name": tool_name, "arguments": arguments},
                        })
                    elif msg_id == 1:
                        if "result" in parsed:
                            result["data"] = parsed["result"]
                        elif "error" in parsed:
                            result["error"] = parsed["error"]
                        done.set()
                        return
        except Exception as e:
            result["error"] = str(e)
            done.set()

    t = threading.Thread(target=listen_sse, daemon=True)
    t.start()
    done.wait(timeout=timeout)

    if result["error"]:
        return f"Error: {result['error']}"
    if result["data"] is None:
        return "Error: timeout waiting for response"

    # Extract text from MCP response
    try:
        content = result["data"]["content"]
        if isinstance(content, list) and content:
            text = content[0].get("text", "")
            # Try to parse as JSON for prettier output
            try:
                parsed = json.loads(text)
                return json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError):
                return text
    except (KeyError, TypeError, IndexError):
        pass

    return json.dumps(result["data"], indent=2)


# --- Identity ---

def get_agent_identifier(username: str) -> str:
    """Deterministic Agent Memory identity for a Moltbook user."""
    return hashlib.sha256(f"moltbook-bridge:{username}".encode()).hexdigest()


# --- Moltbook API ---

def moltbook_request(method: str, path: str, data: dict | None = None) -> dict | list | None:
    url = f"{MOLTBOOK_API}{path}"
    headers = {
        "Authorization": f"Bearer {MOLTBOOK_KEY}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:300]
        log(f"Moltbook {method} {path}: HTTP {e.code} — {body_text}")
        return None
    except Exception as e:
        log(f"Moltbook {method} {path}: {e}")
        return None


def get_notifications() -> list:
    result = moltbook_request("GET", "/notifications")
    if isinstance(result, dict):
        return result.get("notifications", [])
    return result if isinstance(result, list) else []


def get_post_comments(post_id: str) -> list:
    result = moltbook_request("GET", f"/posts/{post_id}/comments")
    if isinstance(result, dict):
        return result.get("comments", [])
    return result if isinstance(result, list) else []


def post_comment(post_id: str, content: str) -> bool:
    result = moltbook_request("POST", f"/posts/{post_id}/comments", {"content": content})
    return result is not None


def already_responded(post_id: str, target_username: str) -> bool:
    """Check if we already posted a response to this user on this post."""
    comments = get_post_comments(post_id)
    for c in comments:
        author = c.get("author", {})
        if author.get("name") == MOLTBOOK_USERNAME:
            # Check if this comment mentions the target user
            content = c.get("content", "")
            if f"@{target_username}" in content:
                return True
    return False


# --- Ensure agent is registered ---

def ensure_registered(username: str, registered: set) -> str:
    """Register a Moltbook user with Agent Memory if needed."""
    identifier = get_agent_identifier(username)
    if username in registered:
        return identifier

    log(f"Registering {username} -> {identifier[:16]}...")
    output = call_agent_memory("memory.register", {
        "agent_identifier": identifier,
        "public_key": f"moltbook-bridge-{username}",
    })
    registered.add(username)
    log(f"Registration: {output[:100]}")
    return identifier


# --- Command parsing ---

def parse_command(text: str) -> dict | None:
    idx = text.find("!memory")
    if idx == -1:
        return None

    rest = text[idx + 7:].strip()
    if not rest:
        return {"action": "help"}

    parts = rest.split(None, 1)
    action = parts[0].lower()
    remainder = parts[1] if len(parts) > 1 else ""

    if action == "help":
        return {"action": "help"}
    elif action == "stats":
        return {"action": "stats"}
    elif action == "store":
        tags = None
        content = remainder
        if content.startswith("#"):
            tag_end = content.find(" ")
            if tag_end > 0:
                tags = content[1:tag_end]
                content = content[tag_end + 1:].strip()
            else:
                tags = content[1:]
                content = ""
        if not content:
            return {"action": "error", "message": "Provide content to store. Usage: `!memory store [#tags] your content`"}
        return {"action": "store", "content": content, "tags": tags}
    elif action == "recall":
        tags = None
        if remainder.startswith("#"):
            tags = remainder[1:].strip()
        return {"action": "recall", "tags": tags}
    elif action == "search":
        if not remainder:
            return {"action": "error", "message": "Provide a search query. Usage: `!memory search <query>`"}
        return {"action": "search", "query": remainder}
    elif action == "propose":
        if not remainder:
            return {"action": "error", "message": "Provide your proposal. Usage: `!memory propose <your proposal>`"}
        return {"action": "commons_contribute", "category": "proposal", "content": remainder}
    elif action == "reply":
        parts2 = remainder.split(None, 1)
        if len(parts2) < 2:
            return {"action": "error", "message": "Usage: `!memory reply <commons_id> <your reply>`"}
        return {"action": "commons_reply", "parent_id": parts2[0], "content": parts2[1]}
    elif action == "thread":
        if not remainder:
            return {"action": "error", "message": "Usage: `!memory thread <commons_id>`"}
        return {"action": "commons_thread", "commons_id": remainder.strip()}
    elif action == "channels":
        if not remainder:
            return {"action": "channel_list"}
        sub_parts = remainder.split(None, 1)
        sub_action = sub_parts[0].lower()
        sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""
        if sub_action == "create":
            name_parts = sub_rest.split(None, 1)
            if not name_parts:
                return {"action": "error", "message": "Usage: `!memory channels create <name> [description]`"}
            return {"action": "channel_create", "name": name_parts[0], "description": name_parts[1] if len(name_parts) > 1 else ""}
        elif sub_action == "join":
            if not sub_rest:
                return {"action": "error", "message": "Usage: `!memory channels join <channel_id>`"}
            return {"action": "channel_join", "channel_id": sub_rest.strip()}
        elif sub_action == "post":
            post_parts = sub_rest.split(None, 1)
            if len(post_parts) < 2:
                return {"action": "error", "message": "Usage: `!memory channels post <channel_id> <content>`"}
            return {"action": "channel_post", "channel_id": post_parts[0], "content": post_parts[1]}
        elif sub_action == "browse":
            if not sub_rest:
                return {"action": "error", "message": "Usage: `!memory channels browse <channel_id>`"}
            return {"action": "channel_browse", "channel_id": sub_rest.strip()}
        elif sub_action == "my":
            return {"action": "channel_my"}
        return {"action": "channel_list"}
    elif action == "dm":
        if not remainder:
            return {"action": "dm_inbox"}
        sub_parts = remainder.split(None, 1)
        sub_action = sub_parts[0].lower()
        sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""
        if sub_action == "send":
            dm_parts = sub_rest.split(None, 1)
            if len(dm_parts) < 2:
                return {"action": "error", "message": "Usage: `!memory dm send <agent_identifier> <message>`"}
            return {"action": "dm_send", "to_identifier": dm_parts[0], "content": dm_parts[1]}
        elif sub_action == "read":
            if not sub_rest:
                return {"action": "error", "message": "Usage: `!memory dm read <agent_identifier>`"}
            return {"action": "dm_read", "other_identifier": sub_rest.strip()}
        return {"action": "dm_inbox"}
    elif action == "commons":
        if not remainder:
            return {"action": "commons_browse"}
        sub_parts = remainder.split(None, 1)
        sub_action = sub_parts[0].lower()
        sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""
        if sub_action == "contribute":
            cat_parts = sub_rest.split(None, 1)
            if len(cat_parts) < 2:
                return {"action": "error", "message": "Usage: `!memory commons contribute <category> <content>`\nCategories: best-practice, pattern, tool-tip, bug-report, feature-request, general, proposal"}
            return {"action": "commons_contribute", "category": cat_parts[0], "content": cat_parts[1]}
        return {"action": "commons_browse"}
    else:
        return {"action": "error", "message": f"Unknown command: `{action}`. Try `!memory help`."}


# --- Command execution ---

def execute_command(cmd: dict, username: str, registered: set) -> str:
    identifier = ensure_registered(username, registered)

    if cmd["action"] == "help":
        return (
            "**Agent Memory Bridge** — your private memory, accessible from Moltbook!\n\n"
            "**Commands:**\n"
            "- `!memory store <content>` — Save a memory\n"
            "- `!memory store #tag1,tag2 <content>` — Save with tags\n"
            "- `!memory recall` — Get recent memories\n"
            "- `!memory recall #tag1,tag2` — Recall by tags\n"
            "- `!memory search <query>` — Search your memories\n"
            "- `!memory commons` — Browse shared agent knowledge\n"
            "- `!memory commons contribute <category> <content>` — Share knowledge\n"
            "- `!memory propose <proposal>` — Submit a proposal for discussion\n"
            "- `!memory reply <id> <text>` — Reply to a contribution\n"
            "- `!memory thread <id>` — View a full discussion thread\n"
            "- `!memory channels` — List topic channels\n"
            "- `!memory channels create <name> [desc]` — Create a channel\n"
            "- `!memory channels join <id>` — Join a channel\n"
            "- `!memory channels post <id> <text>` — Post to a channel\n"
            "- `!memory channels browse <id>` — Browse channel posts\n"
            "- `!memory dm` — Check your inbox\n"
            "- `!memory dm send <agent_id> <msg>` — Send a direct message\n"
            "- `!memory dm read <agent_id>` — Read conversation\n"
            "- `!memory stats` — Your memory statistics\n\n"
            "Your memories are private and encrypted. Only you can access them.\n"
            "Built by @systemadmin_sylex"
        )

    elif cmd["action"] == "error":
        return cmd["message"]

    elif cmd["action"] == "stats":
        output = call_agent_memory("memory.stats", {"agent_identifier": identifier})
        return f"**Your Memory Stats:**\n```\n{output}\n```"

    elif cmd["action"] == "store":
        args = {"agent_identifier": identifier, "encrypted_content": cmd["content"]}
        if cmd.get("tags"):
            args["tags"] = cmd["tags"].split(",")
        output = call_agent_memory("memory.store", args)
        if "error" in output.lower():
            return f"Failed to store: {output[:200]}"
        tag_info = f" Tags: {cmd['tags']}" if cmd.get("tags") else ""
        return f"Memory stored!{tag_info}\nUse `!memory recall` to retrieve your memories."

    elif cmd["action"] == "recall":
        args = {"agent_identifier": identifier}
        if cmd.get("tags"):
            args["tags"] = cmd["tags"].split(",")
        output = call_agent_memory("memory.recall", args)
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated)"
        return f"**Your Memories:**\n```\n{output}\n```"

    elif cmd["action"] == "search":
        output = call_agent_memory("memory.search", {
            "agent_identifier": identifier,
            "query": cmd["query"],
        })
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Search results for '{cmd['query']}':**\n```\n{output}\n```"

    elif cmd["action"] == "commons_browse":
        output = call_agent_memory("commons.browse", {
            "agent_identifier": identifier,
            "limit": 5,
        })
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Commons — Shared Knowledge:**\n{output}"

    elif cmd["action"] == "commons_contribute":
        category = cmd["category"]
        valid = ["best-practice", "pattern", "tool-tip", "bug-report", "feature-request", "general", "proposal"]
        if category not in valid:
            return f"Invalid category: `{category}`\nValid: {', '.join(valid)}"
        output = call_agent_memory("commons.contribute", {
            "agent_identifier": identifier,
            "content": cmd["content"],
            "category": category,
        })
        if "error" in output.lower():
            return f"Contribution failed: {output[:200]}"
        return f"Contributed to the commons! Category: {category}\nOther agents can now see and upvote your knowledge."

    elif cmd["action"] == "channel_list":
        output = call_agent_memory("channels.list", {"agent_identifier": identifier})
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Available Channels:**\n{output}"

    elif cmd["action"] == "channel_create":
        output = call_agent_memory("channels.create", {
            "agent_identifier": identifier,
            "name": cmd["name"],
            "description": cmd.get("description", ""),
        })
        if "error" in output.lower():
            return f"Failed: {output[:200]}"
        return f"Channel **{cmd['name']}** created! Share the channel ID so others can join."

    elif cmd["action"] == "channel_join":
        output = call_agent_memory("channels.join", {
            "agent_identifier": identifier,
            "channel_id": cmd["channel_id"],
        })
        if "error" in output.lower():
            return f"Failed: {output[:200]}"
        return f"Joined the channel! Use `!memory channels post <channel_id> <content>` to contribute."

    elif cmd["action"] == "channel_post":
        output = call_agent_memory("channels.post", {
            "agent_identifier": identifier,
            "channel_id": cmd["channel_id"],
            "content": cmd["content"],
        })
        if "error" in output.lower():
            return f"Failed: {output[:200]}"
        return "Posted to channel!"

    elif cmd["action"] == "channel_browse":
        output = call_agent_memory("channels.browse", {
            "agent_identifier": identifier,
            "channel_id": cmd["channel_id"],
        })
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Channel Posts:**\n{output}"

    elif cmd["action"] == "channel_my":
        output = call_agent_memory("channels.my", {"agent_identifier": identifier})
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Your Channels:**\n{output}"

    elif cmd["action"] == "dm_inbox":
        output = call_agent_memory("agent.inbox", {
            "agent_identifier": identifier,
            "limit": 5,
        })
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Your Inbox:**\n{output}"

    elif cmd["action"] == "dm_send":
        output = call_agent_memory("agent.message", {
            "agent_identifier": identifier,
            "to_identifier": cmd["to_identifier"],
            "content": cmd["content"],
        })
        if "error" in output.lower():
            return f"Failed: {output[:200]}"
        return "Message sent!"

    elif cmd["action"] == "dm_read":
        output = call_agent_memory("agent.conversation", {
            "agent_identifier": identifier,
            "other_identifier": cmd["other_identifier"],
        })
        if len(output) > 1500:
            output = output[:1500] + "\n..."
        return f"**Conversation:**\n{output}"

    elif cmd["action"] == "commons_reply":
        output = call_agent_memory("commons.reply", {
            "agent_identifier": identifier,
            "parent_id": cmd["parent_id"],
            "content": cmd["content"],
        })
        if "error" in output.lower():
            return f"Reply failed: {output[:200]}"
        return f"Reply posted! View the thread with `!memory thread {cmd['parent_id']}`"

    elif cmd["action"] == "commons_thread":
        output = call_agent_memory("commons.thread", {
            "agent_identifier": identifier,
            "commons_id": cmd["commons_id"],
        })
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated)"
        return f"**Thread:**\n{output}"

    return "Something went wrong. Try `!memory help`."


# --- Poll cycle ---

def poll_cycle(registered: set):
    """Run one poll cycle: check notifications, process commands, respond."""
    responses_sent = 0

    notifications = get_notifications()
    log(f"Polling: {len(notifications)} notifications")

    for notif in notifications:
        if responses_sent >= MAX_RESPONSES_PER_RUN:
            log(f"Rate limit: {MAX_RESPONSES_PER_RUN} responses sent, stopping")
            break

        notif_type = notif.get("type", "")
        if notif_type not in ("post_comment", "mention", "comment_reply"):
            continue

        # Check age
        created = notif.get("createdAt", "")
        if created:
            try:
                notif_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - notif_time).total_seconds()
                if age > MAX_MESSAGE_AGE:
                    continue
            except (ValueError, TypeError):
                pass

        post_id = notif.get("relatedPostId", "")
        comment_id = notif.get("relatedCommentId", "")
        if not post_id:
            continue

        # Always fetch the full comment from the post endpoint.
        # The notification's embedded comment object has authorId but NOT
        # the nested author object with the name field.
        comment_data = None
        if comment_id:
            comments = get_post_comments(post_id)
            for c in comments:
                if c.get("id") == comment_id:
                    comment_data = c
                    break

        if not comment_data:
            continue

        content = comment_data.get("content", "")
        author = comment_data.get("author", {})
        username = author.get("name", "")

        if not content or not username or username == MOLTBOOK_USERNAME:
            continue

        if "!memory" not in content:
            continue

        # Stateless dedup: check if we already replied
        if already_responded(post_id, username):
            log(f"Already responded to {username} on post {post_id[:8]}, skipping")
            continue

        log(f"Processing !memory from {username}: {content[:60]}...")

        parsed = parse_command(content)
        if not parsed:
            continue

        response = execute_command(parsed, username, registered)
        reply = f"@{username} {response}"

        if post_comment(post_id, reply):
            responses_sent += 1
            log(f"Responded to {username} via comment")
        else:
            log(f"Failed to respond to {username}")

    # Also check DMs
    dm_result = moltbook_request("GET", "/agents/dm/conversations")
    if isinstance(dm_result, dict):
        convos = dm_result.get("conversations", {})
        items = convos.get("items", []) if isinstance(convos, dict) else convos if isinstance(convos, list) else []
        # DM processing would go here when DMs unlock
        # For now, most Moltbook accounts need 24hr before DMs work

    log(f"Cycle complete: {responses_sent} responses")


# --- Main loop ---

def main():
    if not MOLTBOOK_KEY:
        print("ERROR: MOLTBOOK_API_KEY environment variable required", file=sys.stderr)
        sys.exit(1)

    log(f"Moltbook Memory Bridge starting")
    log(f"Agent Memory: {AGENT_MEMORY_URL}")
    log(f"Poll interval: {POLL_INTERVAL}s")
    log(f"Max message age: {MAX_MESSAGE_AGE}s")

    # Track registered agents in memory (re-registers on restart, which is fine)
    registered: set = set()

    while True:
        try:
            poll_cycle(registered)
        except Exception as e:
            log(f"Poll cycle error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
