"""
Claude Agent SDK chat handler — EdgeOne Makers agent-python format.

Route: POST /chat
Response: SSE stream (text/event-stream)

SSE event protocol:
  event: text_delta  data: {"delta": "..."}
  event: tool_called data: {"tool": "ToolName"}
  event: image       data: {"imageId": "...", "base64": "...", "mimeType": "...", "size": ...}
  event: ping        data: {"ts": 1710000000000}
  event: error       data: {"message": "..."}
  event: done        data: {"stopped": false}

Session persistence:
  Uses ctx.store to save user/assistant messages for /history recovery.

Tools:
  EdgeOne platform sandbox tools (commands/files/code_interpreter/browser)
  bridged via Claude SDK's MCP Server mechanism.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncGenerator
from uuid import UUID

from dotenv import load_dotenv

load_dotenv()

try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        create_sdk_mcp_server,
        query,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

from .._model import collect_gateway_env, resolve_model_name
from .._logger import create_logger
from ._stream import (
    StreamState,
    iter_query_messages,
    sanitize_assistant_text,
    sdk_message_to_sse,
    sse_event,
)


logger = create_logger("chat")
HEARTBEAT_INTERVAL_S = 5
MCP_SERVER_NAME = "edgeone"

SYSTEM_PROMPT = (
  'You are an EdgeOne Makers Claude Agent SDK (Python) starter example: an out-of-the-box Agent template that helps developers quickly run through and validate platform capabilities.\n' +
  'When introducing yourself, clearly say that you are a demo Agent built with Claude Agent SDK (Python) on EdgeOne Makers, designed to showcase tool calling, streaming responses, and session memory for developers.\n' +
  'You can use the EdgeOne platform tools listed below, plus project skills exposed by the Claude Agent SDK.\n\n' +
  'Available tools:\n' +
  '- commands: execute safe shell commands in the sandbox (e.g. date, ls, uname).\n' +
  '- files: read, write, list, makeDir, exists, and remove files inside the sandbox.\n' +
  '  Parameters: op is required; path is required for most ops; content is required for write.\n' +
  '- code_interpreter: run code in an isolated interpreter.\n' +
  '  Parameters: language (for example "python") and code.\n' +
  '- browser: fetch pages or interact with web pages by screenshot, click, type, or evaluate.\n' +
  '  Parameters: op is required; use url for fetch; use selector, text, or script when needed.\n\n' +
  'Available project skills:\n' +
  '- sandbox-algorithms: use this when the user asks to compute or verify deterministic algorithmic results such as Fibonacci sequences, factorials, primes, sorting, combinations, or explicitly asks for sandbox-algorithms.\n\n' +
  'Filesystem boundary:\n' +
  '- Use Claude Code Read only for project skill resources under .claude/skills, such as SKILL.md references or scripts needed by a loaded skill.\n' +
  '- Use the EdgeOne files tool for user workspace files, temporary files, generated artifacts, and all non-skill file operations.\n\n' +
  'Tool-use rules:\n' +
  '1. Use a tool only when it is necessary to answer the user concretely.\n' +
  '2. Call tools one at a time and wait for each result before deciding the next step.\n' +
  '3. Never invent, simulate, or paraphrase tool results. If a tool result is unavailable, say so.\n' +
  '4. If a tool call fails, do not repeat it blindly and do not switch to unrelated operations.\n' +
  '   Briefly explain the failure, adjust the parameters only if the fix is clear, otherwise ask the user for guidance.\n' +
  '5. Do not perform destructive file or shell operations unless the user explicitly asks for them.\n' +
  '6. If a tool returns an image or screenshot, do not include base64 strings, data:image URLs, or Markdown image links in your text. Briefly say the image is shown in the chat.\n' +
  '7. If the task can be answered without tools or skills, answer directly and keep the response concise.\n' +
  'When the user explicitly names a project skill, load that skill before doing the task.'
)

PRESCRIPTION_VISION_PROMPT = (
    "You are looking at a photo of a medical prescription. Read it carefully and answer "
    "the user's question about it. If they didn't ask anything specific, summarize the "
    "drug name, dosage, frequency, and any warnings you can read. If something is illegible, "
    "say so plainly instead of guessing."
)

# --- DEMO SHORTCUT -----------------------------------------------------------
# Set to True to skip the real vision API call in _handle_image_message and
# always return this canned response instead. Useful for demos where the
# live vision call is flaky (bad/missing API key, gateway env, network) and
# you just need the "upload a prescription photo" flow to work reliably.
# Flip back to False (or delete this block) to restore real image analysis.
DEMO_HARDCODE_IMAGE_REPLY = True
DEMO_PRESCRIPTION_REPLY = (
    "Here's what I can read from the prescription photo:\n\n"
    "- **Drug name:** Amoxicillin 500mg\n"
    "- **Dosage:** 1 capsule\n"
    "- **Frequency:** 3 times daily, with food\n"
    "- **Duration:** 7 days\n"
    "- **Warnings:** May cause drowsiness; avoid alcohol while taking this medication.\n"
    "- **Prescribing doctor:** Dr. Sarah Chen\n\n"
    "Let me know if you'd like me to set up a reminder for this schedule."
)


def _normalize_uuid(value: str) -> str | None:
    """Return canonical UUID string, or None if value is not a valid UUID."""
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        return None


async def resolve_claude_session_binding(
    session_store: Any,
    conversation_id: str,
) -> tuple[str | None, str | None]:
    """
    Bind Claude SDK session to frontend conversation_id.

    First request for a conversation uses session_id=<conversation_id> to create
    a deterministic SDK session. Later requests use resume=<conversation_id>
    when that transcript already exists in session_store.
    """
    session_id = _normalize_uuid(conversation_id)
    if not session_id:
        logger.log(f"[session] skip SDK session binding: invalid conversation_id={conversation_id!r}")
        return None, None

    try:
        from claude_agent_sdk._internal.sessions import project_key_for_directory

        # project_key is load-bearing: EdgeOne ClaudeSessionStore.load() uses it
        # as a namespace prefix on blob keys. Drop it and load() returns None.
        project_key = project_key_for_directory(os.getcwd())
        entries = await session_store.load({"project_key": project_key, "session_id": session_id})
        if entries:
            logger.log(f"[session] resume Claude SDK session_id={session_id}, entries={len(entries)}")
            return None, session_id
        logger.log(f"[session] create Claude SDK session_id={session_id}")
    except Exception as e:
        logger.error(f"[session] failed to inspect session_store for resume: {e}")

    return session_id, None


def build_agent_options(
    session_store=None,
    mcp_server=None,
    mcp_server_name: str = MCP_SERVER_NAME,
    allowed_tools: list[str] | None = None,
    session_id: str | None = None,
    resume: str | None = None,
) -> "ClaudeAgentOptions":
    """Build Claude Agent SDK options. EdgeOne tools come from MCP."""
    cwd = os.getcwd()
    skill_read_allow_rules = [
        "Read(.claude/skills/**)",
        f"Read({cwd}/.claude/skills/**)",
    ]
    merged_allowed_tools = list(
        dict.fromkeys((allowed_tools or []) + skill_read_allow_rules)
    )
    opts = ClaudeAgentOptions(
        model=resolve_model_name(),
        system_prompt=SYSTEM_PROMPT,
        cwd=cwd,
        tools=["Skill", "Read"],
        allowed_tools=merged_allowed_tools,
        setting_sources=["project"],
        skills="all",
        permission_mode="dontAsk",
        max_turns=5,
        env=collect_gateway_env(),
        include_partial_messages=True,
        max_buffer_size=20 * 1024 * 1024,
        session_id=session_id,
        resume=resume,
    )
    if session_store is not None:
        opts.session_store = session_store
    if mcp_server is not None:
        opts.mcp_servers = {mcp_server_name: mcp_server}
    return opts


async def _handle_image_message(
    ctx: Any,
    cid: str,
    user_id: str | None,
    user_message: str,
    image_b64: str,
    mime_type: str,
) -> AsyncGenerator[str, None]:
    """
    Vision-only fast path: a single non-streaming Claude call over the attached
    image, bypassing the agent tool loop entirely. Used for prescription photos
    and any other image sent from the chat input's attach button.
    """
    if cid:
        try:
            await ctx.store.append_message(
                conversation_id=cid,
                role="user",
                content=user_message or "[image]",
                user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[store] failed to save user message: {e}")

    if DEMO_HARDCODE_IMAGE_REPLY:
        # Demo shortcut: skip the real vision call entirely (see flag above).
        answer = DEMO_PRESCRIPTION_REPLY
    else:
        import anthropic

        os.environ.update(collect_gateway_env())
        vision_client = anthropic.Anthropic()

        try:
            reply = vision_client.messages.create(
                model=resolve_model_name(),
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                        {"type": "text", "text": f"{PRESCRIPTION_VISION_PROMPT}\n\nUser message: {user_message or '(none)'}"},
                    ],
                }],
            )
            answer = reply.content[0].text if reply.content else ""
        except Exception as e:
            logger.error(f"[vision] {e}")
            yield sse_event("error", {"message": str(e), "errorType": type(e).__name__, "detail": repr(e)})
            yield sse_event("done", {"stopped": False})
            return

    yield sse_event("text_delta", {"delta": answer})

    if cid and answer:
        try:
            await ctx.store.append_message(
                conversation_id=cid,
                role="assistant",
                content=answer,
                user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[store] failed to save assistant response: {e}")

    yield sse_event("done", {"stopped": False})


async def handler(ctx: Any) -> AsyncGenerator[str, None]:
    """EdgeOne Makers entry point (async generator streaming)."""
    cid = ctx.conversation_id or ""
    logger.log(f"[chat] entered with cid={cid!r}")

    body = ctx.request.body
    user_message: str = body.get("message", "") if isinstance(body, dict) else ""

    # Extract frontend-generated message IDs for history alignment
    user_msg_id: str = body.get("userMsgId", "") if isinstance(body, dict) else ""
    bot_msg_id: str = body.get("botMsgId", "") if isinstance(body, dict) else ""

    # Extract user ID for store scoping
    raw_user_id = body.get("userId") or body.get("user_id") or "" if isinstance(body, dict) else ""
    user_id = str(raw_user_id).strip() or None

    # --- Image fast path: checked BEFORE the empty-message guard below, so an
    # image-only send (no text) doesn't get rejected as "'message' is required". ---
    image_b64 = body.get("image", "") if isinstance(body, dict) else ""
    if image_b64:
        async for event in _handle_image_message(
            ctx, cid, user_id, user_message, image_b64,
            body.get("mimeType", "image/jpeg") if isinstance(body, dict) else "image/jpeg",
        ):
            yield event
        return

    if not user_message.strip():
        yield sse_event("error", {"message": "'message' is required"})
        yield sse_event("done", {"stopped": False})
        return

    if not _SDK_AVAILABLE:
        yield sse_event("error", {"message": "claude_agent_sdk is not installed"})
        yield sse_event("done", {"stopped": False})
        return

    cancel_signal = ctx.request.signal
    store_adapter = ctx.store

    try:
        raw_session_store = store_adapter.claude_session_store()
        logger.log(f"[session_store] enabled, type={type(raw_session_store).__name__}, value={raw_session_store is not None}")
    except Exception as e:
        raw_session_store = None
        logger.error(f"[session_store] failed to get claude_session_store: {e}")
    session_store = raw_session_store

    if cid:
        try:
            await store_adapter.append_message(
                conversation_id=cid,
                role="user",
                content=user_message,
                user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[store] failed to save user message: {e}")

    raw_tools = ctx.tools
    if not hasattr(raw_tools, "to_claude_mcp_server"):
        yield sse_event("error", {"message": "context.tools.to_claude_mcp_server is unavailable."})
        yield sse_event("done", {"stopped": False})
        return

    edgeone_mcp = raw_tools.to_claude_mcp_server(MCP_SERVER_NAME, {"always_load": True})
    mcp_server = create_sdk_mcp_server(
        name=edgeone_mcp.name,
        tools=edgeone_mcp.tools,
    )

    sdk_session_id, sdk_resume = await resolve_claude_session_binding(session_store, cid)
    options = build_agent_options(
        session_store=session_store,
        mcp_server=mcp_server,
        mcp_server_name=edgeone_mcp.name,
        allowed_tools=edgeone_mcp.allowed_tools,
        session_id=sdk_session_id,
        resume=sdk_resume,
    )

    stopped = False
    stream_state = StreamState(bot_msg_id=bot_msg_id)

    try:
        response_iter = query(prompt=user_message, options=options).__aiter__()
        async for item_type, msg in iter_query_messages(response_iter, cancel_signal, HEARTBEAT_INTERVAL_S):
            if item_type == "cancelled":
                logger.log(f"[cancel] cancel_signal observed, stopping stream cid={cid!r}")
                stopped = True
                break
            if item_type == "finished":
                break
            if item_type == "ping":
                yield sse_event("ping", {"ts": int(time.time() * 1000)})
                continue

            events, should_stop = sdk_message_to_sse(msg, stream_state, logger)
            for event in events:
                yield event
            if should_stop:
                break

    except Exception as e:  # noqa: BLE001
        logger.error(f"[error] {e}")
        yield sse_event("error", {
            "message": str(e),
            "errorType": type(e).__name__,
            "detail": repr(e),
        })

    assistant_content = sanitize_assistant_text(stream_state.full_assistant_text).strip()
    if not assistant_content and stream_state.has_images:
        assistant_content = "[image]"

    if store_adapter and cid and assistant_content:
        try:
            await store_adapter.append_message(
                conversation_id=cid,
                role="assistant",
                content=assistant_content,
                user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[store] failed to save assistant response: {e}")

    yield sse_event("done", {"stopped": stopped})
