import tempfile
import unittest
from pathlib import Path

from pipeline_agent.loop import STATUS_WAITING_FOR_QUESTION, RunOutcome
from pipeline_agent.session import PendingQuestion, Session
from pipeline_agent.state import escape, parse_answers_file, read_properties, read_state, render_properties, state_field, unescape, write_state


class EscapeTests(unittest.TestCase):
    def test_round_trip(self):
        for value in ("plain", "two\nlines", "back\\slash", "mix\\n literal", "tab\tkeeps", "crlf\r\nend"):
            self.assertEqual(unescape(escape(value)), value.replace("\r", ""))


class StateFileTests(unittest.TestCase):
    def test_write_and_read_question_state(self):
        session = Session(session_id="build-7")
        session.iterations = 3
        session.usage = {"input_tokens": 100, "output_tokens": 20, "model_calls": 3}
        questions = [
            {"question": "Which branch?\nPick one.", "header": "Branch", "options": [{"label": "main", "description": "default"}, {"label": "dev", "description": ""}], "allow_custom": True},
            {"question": "Anything else?", "header": "", "options": [], "allow_custom": True},
        ]
        session.pending_question = PendingQuestion(tool_call_id="call_9", questions=questions)
        outcome = RunOutcome(status=STATUS_WAITING_FOR_QUESTION, session=session, questions=questions)
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, outcome)
            props = read_properties(Path(tmp) / "state.properties")
            self.assertEqual(props["STATUS"], "waiting_for_question")
            self.assertEqual(props["EXIT_CODE"], "10")
            self.assertEqual(props["QUESTION_COUNT"], "2")
            self.assertEqual(props["Q0_TEXT"], "Which branch?\nPick one.")
            self.assertEqual(props["Q0_OPTION_COUNT"], "2")
            self.assertEqual(props["Q0_OPT0_LABEL"], "main")
            self.assertEqual(props["Q1_OPTION_COUNT"], "0")
            state = read_state(tmp)
            self.assertEqual(state["pending_tool_call_id"], "call_9")
            self.assertEqual(state_field(state, "ITERATIONS"), "3")
            self.assertEqual(state_field(state, "Q0_HEADER"), "Branch")
            self.assertIsNone(state_field(state, "MISSING"))
            # Every property line is a single line.
            text = (Path(tmp) / "state.properties").read_text(encoding="utf-8")
            self.assertTrue(all("=" in line for line in text.strip().splitlines()))

    def test_properties_rendering_is_single_line(self):
        state = {"status": "completed", "exit_code": 0, "session_id": "s", "iterations": 1, "usage": {}, "final_message": "a\nb", "error": "", "questions": []}
        rendered = render_properties(state)
        self.assertIn("FINAL_MESSAGE=a\\nb", rendered)


class AnswersFileTests(unittest.TestCase):
    def test_parse_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answers.properties"
            path.write_text("Q0=main\nQ1=\nQ1_TEXT=custom\\nmultiline\nQ2=[other: see text answer]\nQ2_TEXT=typed\n", encoding="utf-8")
            answers = parse_answers_file(path)
            self.assertEqual(answers[0], "main")
            self.assertEqual(answers[1], "custom\nmultiline")
            self.assertEqual(answers[2], "typed")


if __name__ == "__main__":
    unittest.main()
