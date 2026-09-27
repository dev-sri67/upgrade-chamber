"""Tests for the bounded tool-loop agent session."""

import hashlib
import io
import unittest
import zipfile

from upgrade_chamber.agent import (
    MAX_MESSAGES,
    MAX_READ_FILE_CHARS,
    AgentResult,
    AgentSession,
    AgentSessionError,
    AgentTurnRecord,
)
from upgrade_chamber.edits import member_name
from test_edits import (
    ADAPTERS_PATH,
    TEST_FILE_PATH,
    edit_for,
    source_zip,
)


SAMPLE_LOG = (
    "_____________________________ test_connect _____________________________\n"
    "requests_unixsocket/tests/test_requests_unixsocket.py:42: in test_connect\n"
    "E   gaierror: [Errno -2] Name or service not known\n"
    "=========================== 1 failed in 0.12s ==========================\n"
)

NOTE_TEXT = (
    "Note: older tool results were removed to keep this conversation within "
    "the message bound; earlier tool results are no longer visible here."
)


def make_session(responses, *, test_log=SAMPLE_LOG, usages=None,
                 max_turns=10, max_messages=MAX_MESSAGES, source_zip_bytes=None):
    """Build a session whose model_call pops scripted responses per call."""
    captured: list[list[dict]] = []
    state = {"index": 0}

    def model_call(messages):
        captured.append([dict(message) for message in messages])
        index = state["index"]
        state["index"] += 1
        if index >= len(responses):
            payload = {"message": {"tool": "abort", "args": {"reason": "script exhausted"}}}
        else:
            payload = {"message": responses[index]}
        if usages is not None and index < len(usages) and usages[index] is not None:
            payload["usage"] = usages[index]
        return payload

    session = AgentSession(
        source_zip=source_zip_bytes if source_zip_bytes is not None else source_zip(),
        test_log=test_log,
        model_call=model_call,
        max_turns=max_turns,
        max_messages=max_messages,
    )
    return session, captured


def turn_response(tool, args):
    return {"tool": tool, "args": args}


VALID_EDIT = edit_for(
    ADAPTERS_PATH,
    "HTTPAdapter = object\nUNIXSocketAdapter = object  # repaired\n",
)
WRONG_HASH_EDIT = dict(VALID_EDIT, original_sha256="0" * 64)


class AgentSessionTest(unittest.TestCase):
    def test_opening_system_message_demands_brief_reasoning(self):
        session, captured = make_session([turn_response("abort", {"reason": "stop"})])
        session.run()
        system_content = captured[0][0]["content"]
        self.assertEqual(captured[0][0]["role"], "system")
        self.assertIn("Reason briefly", system_content)
        self.assertIn(
            "the first turn should usually be list_repo_files or read_file",
            system_content,
        )
        self.assertIn("read_file reports the file's current sha256", system_content)
        self.assertIn("use that exact value as original_sha256", system_content)

    def test_happy_repair_finishes_with_validated_edits(self):
        responses = [
            turn_response("list_repo_files", {}),
            turn_response("read_file", {"path": ADAPTERS_PATH}),
            turn_response("propose_edits", {"edits": [VALID_EDIT]}),
            turn_response("read_test_log", {}),
            turn_response("finish", {"summary": "Repaired adapter.", "edits": [VALID_EDIT]}),
        ]
        usages = [
            {"prompt_tokens": 10, "completion_tokens": 5},
            {"prompt_tokens": 7, "completion_tokens": 3},
            None,
            None,
            None,
        ]
        session, captured = make_session(responses, usages=usages)

        result = session.run()

        self.assertIsInstance(result, AgentResult)
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.summary, "Repaired adapter.")
        self.assertEqual(result.edits, [VALID_EDIT])
        self.assertEqual(result.model_calls, 5)
        self.assertEqual(result.usage, {"prompt_tokens": 17, "completion_tokens": 8})

        self.assertEqual(
            [(turn.index, turn.tool, turn.ok) for turn in result.turns],
            [
                (1, "list_repo_files", True),
                (2, "read_file", True),
                (3, "propose_edits", True),
                (4, "read_test_log", True),
                (5, "finish", True),
            ],
        )
        for turn in result.turns:
            self.assertIsInstance(turn, AgentTurnRecord)
            self.assertLessEqual(len(turn.detail), 500)

        # Opening framing: allow-list and member names, log tail.
        opening = captured[0][1]["content"]
        self.assertIn("Editable paths", opening)
        self.assertIn(ADAPTERS_PATH, opening)
        self.assertIn("Name or service not known", opening)
        # Tool result after list_repo_files includes member names.
        listing_result = captured[1][-1]["content"]
        self.assertIn(ADAPTERS_PATH, listing_result)
        self.assertIn(member_name("setup.py"), listing_result)
        # read_file delivered content.
        self.assertIn("HTTPAdapter = object", captured[2][-1]["content"])
        # propose_edits static pass plus controller-only reminder.
        proposal_result = captured[3][-1]["content"]
        self.assertIn(
            "static validation passed for 1 edit(s): requests_unixsocket/adapters.py",
            proposal_result,
        )
        self.assertIn("only the controller", proposal_result)
        # read_test_log delivered the stored log.
        self.assertIn("1 failed in 0.12s", captured[4][-1]["content"])

    def test_finish_with_invalid_edits_continues_then_aborts(self):
        responses = [
            turn_response("finish", {"summary": "Repair.", "edits": [WRONG_HASH_EDIT]}),
            turn_response("abort", {"reason": "cannot repair safely"}),
        ]
        session, captured = make_session(responses)

        result = session.run()

        self.assertEqual(result.status, "aborted")
        self.assertEqual(result.summary, "cannot repair safely")
        self.assertEqual(result.edits, [])
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(
            [(turn.tool, turn.ok) for turn in result.turns],
            [("finish", False), ("abort", True)],
        )
        self.assertIn("does not match", result.turns[0].detail)
        self.assertIn("finish rejected", captured[1][-1]["content"])

    def test_invalid_turn_shapes_end_session(self):
        cases = [
            {"tool": "list_repo_files"},                              # missing args
            {"tool": "list_repo_files", "args": {}, "extra": True},   # extra keys
            "I will help with the repair.",                           # non-dict message
            {"tool": 7, "args": {}},                                  # non-string tool
            {"tool": "list_repo_files", "args": []},                  # non-dict args
        ]
        for message in cases:
            with self.subTest(message=message):
                session, _ = make_session([message])
                result = session.run()
                self.assertEqual(result.status, "invalid")
                self.assertIsNone(result.summary)
                self.assertEqual(result.edits, [])
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(len(result.turns), 1)
                self.assertIsNone(result.turns[0].tool)
                self.assertFalse(result.turns[0].ok)

    def test_unknown_tool_ends_session_invalid(self):
        session, _ = make_session([turn_response("run_shell", {"cmd": "ls"})])
        result = session.run()
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.turns[0].tool, "run_shell")
        self.assertFalse(result.turns[0].ok)
        self.assertIn("unknown tool", result.turns[0].detail)

    def test_turn_budget_exhaustion(self):
        responses = [turn_response("list_repo_files", {}) for _ in range(3)]
        session, _ = make_session(responses, max_turns=3)
        result = session.run()
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.summary, "turn budget exhausted")
        self.assertEqual(result.edits, [])
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(len(result.turns), 3)
        self.assertTrue(all(turn.ok for turn in result.turns))

    def test_read_file_appends_exact_sha256_line(self):
        responses = [
            turn_response("read_file", {"path": ADAPTERS_PATH}),
            turn_response("abort", {"reason": "stop"}),
        ]
        session, captured = make_session(responses)
        session.run()
        tool_result = captured[1][-1]["content"]
        # The final line must carry the exact hash the edit validator expects.
        expected_line = (
            f"[file sha256: {VALID_EDIT['original_sha256']}]"
            " - use this exact value as original_sha256 when proposing an edit to this file"
        )
        self.assertEqual(tool_result.splitlines()[-1], expected_line)
        self.assertIn("HTTPAdapter = object", tool_result)

    def test_read_file_reports_full_content_sha256_even_when_truncated(self):
        # Live run 11's proposal was rejected because the model could not see
        # any hash; the appended line must survive truncation and carry the
        # hash of the FULL content, not the delivered prefix.
        large_bytes = ("a" * (MAX_READ_FILE_CHARS + 1000) + "\n").encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                zipfile.ZipInfo(member_name(ADAPTERS_PATH), date_time=(1980, 1, 1, 0, 0, 0)),
                large_bytes,
            )
        responses = [
            turn_response("read_file", {"path": ADAPTERS_PATH}),
            turn_response("abort", {"reason": "stop"}),
        ]
        session, captured = make_session(responses, source_zip_bytes=output.getvalue())
        session.run()
        tool_result = captured[1][-1]["content"]
        self.assertIn("[truncated]", tool_result)
        expected_line = (
            f"[file sha256: {hashlib.sha256(large_bytes).hexdigest()}]"
            " - use this exact value as original_sha256 when proposing an edit to this file"
        )
        self.assertEqual(tool_result.splitlines()[-1], expected_line)

    def test_read_file_missing_member_continues_to_abort(self):
        responses = [
            turn_response("read_file", {"path": "does/not/exist.py"}),
            turn_response("abort", {"reason": "file missing"}),
        ]
        session, captured = make_session(responses)
        result = session.run()
        self.assertEqual(result.status, "aborted")
        self.assertFalse(result.turns[0].ok)
        self.assertIn("not present in source", result.turns[0].detail)
        self.assertIn("not present in source", captured[1][-1]["content"])

    def test_read_test_log_without_log_is_tool_error(self):
        responses = [
            turn_response("read_test_log", {}),
            turn_response("abort", {"reason": "no log"}),
        ]
        session, captured = make_session(responses, test_log=None)
        result = session.run()
        self.assertEqual(result.status, "aborted")
        self.assertFalse(result.turns[0].ok)
        self.assertEqual(result.turns[0].tool, "read_test_log")
        self.assertIn("no test log available", captured[1][-1]["content"])

    def test_propose_edits_rejection_continues_session(self):
        responses = [
            turn_response("propose_edits", {"edits": [WRONG_HASH_EDIT]}),
            turn_response("abort", {"reason": "giving up"}),
        ]
        session, captured = make_session(responses)
        result = session.run()
        self.assertEqual(result.status, "aborted")
        self.assertFalse(result.turns[0].ok)
        self.assertIn("static validation rejected", captured[1][-1]["content"])

    def test_conversation_compaction_drops_oldest_tool_results(self):
        paths = [
            ADAPTERS_PATH,
            "setup.py",
            "requests_unixsocket/__init__.py",
            TEST_FILE_PATH,
            ADAPTERS_PATH,
            "setup.py",
        ]
        responses = [turn_response("read_file", {"path": path}) for path in paths]
        session, captured = make_session(responses, max_turns=6, max_messages=6)

        result = session.run()

        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.model_calls, 6)

        # The system message always leads.
        for snapshot in captured:
            self.assertEqual(snapshot[0]["role"], "system")
            self.assertLessEqual(len(snapshot), 7)  # bound + one compaction note

        # Opening framing still present before compaction, dropped after.
        self.assertIn("Editable paths", captured[0][1]["content"])
        self.assertNotIn("Editable paths", captured[3][-1]["content"])

        # Turn-one tool result survives the first compaction, is dropped later.
        self.assertIn("HTTPAdapter = object", captured[3][1]["content"])
        self.assertNotIn(
            "HTTPAdapter = object", "".join(m["content"] for m in captured[4])
        )
        self.assertIn("test_placeholder", captured[4][-1]["content"])

        # The honest compaction note appears exactly once per snapshot.
        for position, snapshot in enumerate(captured):
            count = sum(
                1 for message in snapshot if NOTE_TEXT in message["content"]
            )
            if position < 3:
                self.assertEqual(count, 0, f"snapshot {position}")
            else:
                self.assertEqual(count, 1, f"snapshot {position}")

    def test_usage_accumulation_with_missing_usage(self):
        responses = [
            turn_response("list_repo_files", {}),
            turn_response("finish", {"summary": "Done.", "edits": [VALID_EDIT]}),
        ]
        usages = [
            {"prompt_tokens": 10, "completion_tokens": 5},
            {"prompt_tokens": 7, "completion_tokens": 3},
        ]
        session, _ = make_session(responses, usages=usages)
        result = session.run()
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.usage, {"prompt_tokens": 17, "completion_tokens": 8})

        # Missing or None usage entries sum as zero.
        responses = [
            turn_response("list_repo_files", {}),
            turn_response("abort", {"reason": "stop"}),
        ]
        session, _ = make_session(responses)
        result = session.run()
        self.assertEqual(result.status, "aborted")
        self.assertIsNone(result.usage)

    def test_invalid_constructor_bounds_raise(self):
        with self.assertRaises(AgentSessionError):
            AgentSession(
                source_zip=source_zip(),
                test_log=None,
                model_call=lambda messages: {"message": {"tool": "abort", "args": {"reason": "x"}}},
                max_turns=0,
            )
        with self.assertRaises(AgentSessionError):
            AgentSession(
                source_zip=source_zip(),
                test_log=None,
                model_call=lambda messages: {"message": {"tool": "abort", "args": {"reason": "x"}}},
                max_messages=1,
            )

    def test_corrupt_source_zip_raises_session_error(self):
        session = AgentSession(
            source_zip=b"not a zip",
            test_log=None,
            model_call=lambda messages: {"message": {"tool": "abort", "args": {"reason": "x"}}},
        )
        with self.assertRaises(AgentSessionError):
            session.run()


if __name__ == "__main__":
    unittest.main()
