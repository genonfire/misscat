"""Default output shows review status and every warning/error; --loud adds reviewer tool activity."""
import io
import logging
import threading
import unittest

import misscat as m


class ActivityLogTests(unittest.TestCase):
    def capture(self, loud, emit, thread_name=None):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(threadName)s %(message)s"))
        levels = m.log.level, m.activity_log.level
        propagate, m.log.propagate = m.log.propagate, False
        m.log.addHandler(handler)
        try:
            m._configure_logging(loud)
            if thread_name:
                thread = threading.Thread(target=emit, name=thread_name)
                thread.start()
                thread.join()
            else:
                emit()
            return stream.getvalue()
        finally:
            m.log.removeHandler(handler)
            m.log.propagate = propagate
            m.log.setLevel(levels[0])
            m.activity_log.setLevel(levels[1])

    def both(self, emit, **kwargs):
        return self.capture(False, emit, **kwargs), self.capture(True, emit, **kwargs)

    def test_codex(self):
        events = [
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "git diff"}},
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 2}},
            {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "gh", "tool": "pr"}},
            {"type": "item.completed",
             "item": {"type": "mcp_tool_call", "server": "gh", "tool": "pr", "status": "completed"}},
            {"type": "item.completed",
             "item": {"type": "mcp_tool_call", "server": "gh", "tool": "pr", "status": "failed"}},
            {"type": "item.started", "item": {"type": "web_search", "query": "python logging"}},
            {"type": "item.completed", "item": {"type": "web_search"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
            {"type": "turn.failed", "error": {"message": "boom"}},
        ]
        default, loud = self.both(lambda: [m._log_codex_event(e) for e in events])
        for out in (default, loud):
            self.assertIn("INFO MainThread reviewer: working", out)
            self.assertIn("WARNING MainThread exec: completed (2)", out)
            self.assertIn("WARNING MainThread mcp: gh.pr failed", out)
            self.assertIn("reviewer: turn done (input=10, output=3)", out)
            self.assertIn("ERROR MainThread reviewer: boom", out)
        for line in ("exec: git diff", "exec: completed (0)", "mcp: gh.pr started",
                     "mcp: gh.pr completed", "web search: python logging", "web search: completed"):
            self.assertNotIn(line, default)
            self.assertIn(f"INFO MainThread {line}", loud)

    def test_claude(self):
        tools = {}
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "x.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a"},
                {"type": "tool_result", "tool_use_id": "b", "is_error": True},
            ]}},
            {"type": "result", "num_turns": 4, "total_cost_usd": 0.5},
            {"type": "result", "is_error": True},
        ]
        default, loud = self.both(lambda: [m._log_claude_event(e, tools) for e in events])
        for out in (default, loud):
            self.assertIn("reviewer: claude initialized", out)
            self.assertIn("WARNING MainThread tool: Read failed", out)
            self.assertIn("reviewer: claude done (turns=4, cost=$0.5000)", out)
            self.assertIn("ERROR MainThread reviewer: claude result error", out)
        for line in ("tool: Bash: ls", "tool: Read: x.py", "tool: Bash completed"):
            self.assertNotIn(line, default)
            self.assertIn(f"INFO MainThread {line}", loud)

    def test_gemini(self):
        events = [
            {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE"}},
            {"event": "step_update", "step_update": {
                "step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                "tool_info": {"parameters": {"CommandLine": "git log"}}}},
            {"event": "step_update", "step_update": {
                "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file"}},
            {"event": "result", "result": {"usage": {"total_tokens": 42}}},
        ]
        default, loud = self.both(lambda: [m._log_gemini_event(e) for e in events])
        for out in (default, loud):
            self.assertIn("reviewer: working", out)
            self.assertIn("reviewer: done (tokens=42)", out)
        for line in ("reviewer: run_command git log", "reviewer: view_file"):
            self.assertNotIn(line, default)
            self.assertIn(f"INFO MainThread {line}", loud)

    def test_raw_cli_debug_only_when_loud(self):
        default, loud = self.both(lambda: m.log.debug("codex: raw line"))
        self.assertNotIn("raw line", default)
        self.assertIn("DEBUG MainThread codex: raw line", loud)

    def test_cat_threads_share_the_configuration(self):
        def emit():
            m._log_codex_event({"type": "turn.started"})
            m._log_codex_event({"type": "item.started",
                                "item": {"type": "command_execution", "command": "ls"}})
            m._log_codex_event({"type": "item.completed",
                                "item": {"type": "command_execution", "status": "failed"}})

        default, loud = self.both(emit, thread_name="cat-2")
        for out in (default, loud):
            self.assertIn("INFO cat-2 reviewer: working", out)
            self.assertIn("WARNING cat-2 exec: failed", out)
        self.assertNotIn("exec: ls", default)
        self.assertIn("INFO cat-2 exec: ls", loud)


if __name__ == "__main__":
    unittest.main()
