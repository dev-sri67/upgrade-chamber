"""Bounded tool-loop agent session for compatibility repair.

The model iterates with a fixed tool set and the controller executes every
tool server-side. The model never runs commands, never sees credentials, and
never decides whether tests passed: execution verdicts come only from
controller-run containers, and propose_edits feedback is static validation
only. No I/O, no inference, stdlib only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import zipfile
from typing import Callable

from . import edits
from .edits import ALLOWED_EDIT_PATHS, EditValidationError, member_name


# Live run 11 explored for 8 real tool turns with minimax-m3 and then had its
# single finish proposal rejected on a stale hash; two extra turns give the
# model room to re-read a file and re-propose without a second execution.
MAX_TURNS = 10                    # model turns per session
MAX_TOOL_RESULT_CHARS = 24000     # per tool result
MAX_READ_FILE_CHARS = 24000
MAX_TEST_LOG_CHARS = 20000
MAX_MESSAGES = 24                 # conversation bound handed to the transport

_SUMMARY_LIMIT = 2000
_DETAIL_LIMIT = 500
_TRUNCATION_MARKER = "[truncated]"
_COMPACTION_NOTE = (
    "Note: older tool results were removed to keep this conversation within "
    "the message bound; earlier tool results are no longer visible here."
)

# Live probing showed unbounded reasoning starves the completion budget, so the
# session asks the model for short reasoning and immediate tool use.
_SYSTEM_PROMPT = (
    "You are a compatibility repair agent working inside a controlled "
    "upgrade pipeline. You interact only through a fixed tool set executed "
    "by the controller; you cannot run commands, alter limits, or see "
    "credentials. Reply with exactly one JSON object per turn and nothing "
    "else:\n"
    '- {"tool": "<name>", "args": {...}} to call one of the listed tools;\n'
    '- {"tool": "finish", "args": {"summary": "...", "edits": [...]}} to end '
    "with a repair;\n"
    '- {"tool": "abort", "args": {"reason": "..."}} to give up honestly.\n'
    "Available tools: list_repo_files, read_file, read_test_log, "
    "propose_edits, finish, abort.\n"
    "Each edit entry must be a dict with exactly path, original_sha256, and "
    "replacement_text, where path comes from the controller-provided "
    "editable allow-list and original_sha256 matches the current content. "
    "read_file reports the file's current sha256; use that exact value as "
    "original_sha256 when editing that file. "
    "Never claim tests passed; only the controller executes tests and "
    "decides outcomes. "
    "Reason briefly. Call exactly one tool per turn. Do not overthink; the "
    "first turn should usually be list_repo_files or read_file."
)


class AgentSessionError(RuntimeError):
    """The agent session ended in a way safe to record honestly."""


@dataclasses.dataclass(frozen=True)
class AgentTurnRecord:
    index: int
    tool: str | None          # tool the model invoked, or None when the model finished/aborted
    ok: bool                  # did the tool call execute successfully (statically)
    detail: str               # bounded human-readable outcome (<= 500 chars)


@dataclasses.dataclass(frozen=True)
class AgentResult:
    status: str                # "finished" | "aborted" | "exhausted" | "invalid"
    summary: str | None        # model finish summary (<= 2000 chars) or abort reason
    edits: list[dict]         # final validated edits (may be empty)
    turns: list[AgentTurnRecord]
    model_calls: int
    usage: dict | None         # summed provider usage when available


def _bound_detail(text: str) -> str:
    if len(text) <= _DETAIL_LIMIT:
        return text
    return text[: _DETAIL_LIMIT - 3] + "..."


def _cap_head(text: str, limit: int) -> str:
    """Return text capped from the start with an explicit truncation marker."""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARKER) - 1)
    return text[:keep] + "\n" + _TRUNCATION_MARKER


def _cap_tail(text: str, limit: int) -> str:
    """Return the tail of text capped with an explicit truncation marker."""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARKER) - 1)
    return _TRUNCATION_MARKER + "\n" + text[-keep:]


def _accumulate_usage(total: dict[str, int], usage: object) -> bool:
    """Add provider usage into the running sum; return True when usage was seen."""
    if not isinstance(usage, dict):
        return False
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] += value
    return True


class AgentSession:
    """One bounded repair session: fixed tools, controller-side execution."""

    def __init__(
        self,
        *,
        source_zip: bytes,
        test_log: str | None,
        model_call: Callable[[list[dict[str, str]]], dict],
        validate_edits_fn: Callable[[bytes, object], list[dict]] = edits.validate_edits,
        max_turns: int = MAX_TURNS,
        max_messages: int = MAX_MESSAGES,
    ) -> None:
        if max_turns < 1:
            raise AgentSessionError("max_turns must be at least 1")
        if max_messages < 2:
            raise AgentSessionError("max_messages must leave room beyond the system message")
        self.source_zip = source_zip
        self.test_log = test_log
        self.model_call = model_call
        self.validate_edits_fn = validate_edits_fn
        self.max_turns = max_turns
        self.max_messages = max_messages

    # -- opening context ---------------------------------------------------

    def _member_names(self) -> list[str]:
        try:
            with zipfile.ZipFile(io.BytesIO(self.source_zip)) as archive:
                return [info.filename for info in archive.infolist()]
        except zipfile.BadZipFile as exc:
            raise AgentSessionError(
                "source archive is not a readable zip; session cannot proceed"
            ) from exc

    def _opening_message(self) -> str:
        listing = _cap_head("\n".join(self._member_names()), MAX_TOOL_RESULT_CHARS)
        if self.test_log is None:
            log_section = "(no test log available)"
        else:
            log_section = _cap_tail(self.test_log, MAX_TEST_LOG_CHARS)
        allow_list = "\n".join(f"- {path}" for path in ALLOWED_EDIT_PATHS)
        return (
            "You are repairing a compatibility failure in a staged source "
            "archive. Controller framing follows.\n\n"
            "Repository files (member names only, bounded):\n"
            f"{listing}\n\n"
            "Latest failing test log (tail, bounded):\n"
            f"{log_section}\n\n"
            "Editable paths (the only paths propose_edits may target):\n"
            f"{allow_list}\n\n"
            "Call one tool now."
        )

    # -- tool execution ----------------------------------------------------

    def _tool_list_repo_files(self, args: dict) -> tuple[str, bool, str]:
        if args != {}:
            return "list_repo_files takes no arguments", False, "list_repo_files takes no arguments"
        names = self._member_names()
        return (
            _cap_head("\n".join(names), MAX_TOOL_RESULT_CHARS),
            True,
            f"listed {len(names)} source member name(s)",
        )

    def _tool_read_file(self, args: dict) -> tuple[str, bool, str]:
        path = args.get("path")
        if not isinstance(path, str):
            return "read_file requires a string path", False, "read_file requires a string path"
        name = member_name(path)
        try:
            with zipfile.ZipFile(io.BytesIO(self.source_zip)) as archive:
                try:
                    content = archive.read(name)
                except KeyError:
                    reason = f"file not present in source: {path}"
                    return reason, False, reason
        except zipfile.BadZipFile as exc:
            raise AgentSessionError(
                "source archive is not a readable zip; session cannot proceed"
            ) from exc
        text = content.decode("utf-8", errors="replace")
        # Live run 11's finish proposal was rejected solely because
        # original_sha256 did not match: read_file showed only content, so the
        # model had to guess the hash. The digest is computed from the full
        # member bytes before truncation so the model can copy an exact value.
        result = (
            _cap_head(text, MAX_READ_FILE_CHARS)
            + "\n"
            + "[file sha256: "
            + hashlib.sha256(content).hexdigest()
            + "] - use this exact value as original_sha256 when proposing an edit to this file"
        )
        return (
            result,
            True,
            f"read {path} ({len(text)} chars)",
        )

    def _tool_read_test_log(self, args: dict) -> tuple[str, bool, str]:
        if args != {}:
            return "read_test_log takes no arguments", False, "read_test_log takes no arguments"
        if self.test_log is None:
            reason = "no test log available"
            return reason, False, reason
        tail = _cap_tail(self.test_log, MAX_TEST_LOG_CHARS)
        return tail, True, f"returned test log tail ({len(self.test_log)} chars)"

    def _tool_propose_edits(self, args: dict) -> tuple[str, bool, str]:
        if "edits" not in args:
            return "propose_edits requires an edits list", False, "propose_edits requires an edits list"
        try:
            validated = self.validate_edits_fn(self.source_zip, args["edits"])
        except EditValidationError as exc:
            reason = f"static validation rejected: {exc}"
            return reason, False, reason
        paths = ", ".join(str(edit["path"]) for edit in validated)
        result = (
            f"static validation passed for {len(validated)} edit(s): {paths}\n"
            "Reminder: this is static validation only; only the controller's "
            "container execution decides pass/fail."
        )
        return result, True, f"static validation passed for {len(validated)} edit(s): {paths}"

    # -- session -----------------------------------------------------------

    def run(self) -> AgentResult:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": self._opening_message()},
        ]
        turns: list[AgentTurnRecord] = []
        model_calls = 0
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        usage_seen = False
        compaction_noted = False

        def usage_or_none() -> dict | None:
            return dict(usage_total) if usage_seen else None

        for index in range(1, self.max_turns + 1):
            # 1. Bound the conversation, keeping the system message.
            if len(messages) > self.max_messages:
                messages = [messages[0]] + messages[-(self.max_messages - 1):]
                if not compaction_noted:
                    messages.append({"role": "user", "content": _COMPACTION_NOTE})
                    compaction_noted = True

            # 2. One structured model call over the full current conversation.
            model_calls += 1
            response = self.model_call(messages)
            if isinstance(response, dict):
                usage_seen = _accumulate_usage(usage_total, response.get("usage")) or usage_seen
            if not isinstance(response, dict) or not isinstance(response.get("message"), dict):
                turns.append(AgentTurnRecord(
                    index, None, False,
                    _bound_detail("model response lacked a valid message object"),
                ))
                return AgentResult("invalid", None, [], turns, model_calls, usage_or_none())
            msg = response["message"]

            # 3. Strict turn shape: exactly {"tool", "args"}.
            if (
                set(msg) != {"tool", "args"}
                or not isinstance(msg["tool"], str)
                or not isinstance(msg["args"], dict)
            ):
                turns.append(AgentTurnRecord(
                    index, None, False,
                    _bound_detail("model turn was not exactly {tool, args} with a string tool and dict args"),
                ))
                return AgentResult("invalid", None, [], turns, model_calls, usage_or_none())
            tool, args = msg["tool"], msg["args"]

            # 4. Dispatch the fixed tool set.
            if tool == "list_repo_files":
                result, ok, detail = self._tool_list_repo_files(args)
            elif tool == "read_file":
                result, ok, detail = self._tool_read_file(args)
            elif tool == "read_test_log":
                result, ok, detail = self._tool_read_test_log(args)
            elif tool == "propose_edits":
                result, ok, detail = self._tool_propose_edits(args)
            elif tool == "finish":
                summary = args.get("summary")
                if not isinstance(summary, str) or not 1 <= len(summary) <= _SUMMARY_LIMIT:
                    reason = "finish summary must be a string of 1 to 2000 characters"
                    result, ok, detail = reason, False, reason
                else:
                    try:
                        validated = self.validate_edits_fn(self.source_zip, args.get("edits"))
                    except EditValidationError as exc:
                        reason = f"finish rejected: {exc}"
                        result, ok, detail = reason, False, reason
                    else:
                        turns.append(AgentTurnRecord(
                            index, "finish", True,
                            _bound_detail(f"finished with {len(validated)} validated edit(s)"),
                        ))
                        return AgentResult(
                            "finished", summary, list(validated), turns,
                            model_calls, usage_or_none(),
                        )
            elif tool == "abort":
                reason_text = args.get("reason")
                if not isinstance(reason_text, str) or not 1 <= len(reason_text) <= _SUMMARY_LIMIT:
                    reason = "abort reason must be a string of 1 to 2000 characters"
                    result, ok, detail = reason, False, reason
                else:
                    turns.append(AgentTurnRecord(
                        index, "abort", True, _bound_detail(f"aborted: {reason_text}"),
                    ))
                    return AgentResult(
                        "aborted", reason_text, [], turns, model_calls, usage_or_none(),
                    )
            else:
                # 5. Unknown tool: the session ends, safe to record honestly.
                turns.append(AgentTurnRecord(
                    index, tool, False, _bound_detail(f"unknown tool: {tool}"),
                ))
                return AgentResult("invalid", None, [], turns, model_calls, usage_or_none())

            turns.append(AgentTurnRecord(index, tool, ok, _bound_detail(detail)))
            messages.append({"role": "assistant", "content": json.dumps(msg)})
            messages.append({"role": "user", "content": result})

        return AgentResult(
            "exhausted", "turn budget exhausted", [], turns, model_calls, usage_or_none(),
        )
