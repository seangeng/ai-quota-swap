"""prompts.py: a question is only asked on a terminal; cancelling is a no."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from ai_quota_swap import prompts
from ai_quota_swap.exceptions import ConfirmationRequiredError, ValidationError


def test_off_a_terminal_it_names_the_flag():
    with patch.object(prompts, "interactive", return_value=False):
        with pytest.raises(ConfirmationRequiredError) as exc:
            prompts.confirm("Remove it?", command="qswap remove")
    assert str(exc.value) == "qswap remove needs --yes when there is no terminal to ask on"


def test_assume_yes_never_asks():
    with patch.object(prompts, "interactive", return_value=False), \
         patch.object(prompts, "read_line", side_effect=AssertionError("asked")):
        assert prompts.confirm("Remove it?", command="qswap remove", assume_yes=True)


@pytest.mark.parametrize("answer, result", [("y", True), ("YES", True), ("n", False), ("", False)])
def test_answers(answer, result):
    with patch.object(prompts, "interactive", return_value=True), \
         patch.object(prompts, "read_line", return_value=answer):
        assert prompts.confirm("Remove it?", command="qswap remove") is result


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_end_of_input_or_ctrl_c_at_the_prompt_is_a_no(error, capsys):
    with patch.object(prompts, "interactive", return_value=True), \
         patch.object(prompts, "read_line", side_effect=error):
        assert prompts.confirm("Remove it?", command="qswap remove") is False


def test_choose_off_a_terminal_says_how_to_name_one():
    with patch.object(prompts, "interactive", return_value=False):
        with pytest.raises(ConfirmationRequiredError, match="name one by number"):
            prompts.choose("Which?", ["2", "5"], unasked="x matches 2 and 5; name one by number")


def test_choose_rejects_anything_else():
    with patch.object(prompts, "interactive", return_value=True), \
         patch.object(prompts, "read_line", return_value="7"):
        assert prompts.choose("Which?", ["2", "5"], unasked="") is None


def test_secret_reads_stdin_off_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-ant-api03-x\n"))
    with patch.object(prompts, "interactive", return_value=False):
        assert prompts.secret("Token: ", unasked="pipe it in") == "sk-ant-api03-x"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with patch.object(prompts, "interactive", return_value=False):
        with pytest.raises(ValidationError, match="pipe it in"):
            prompts.secret("Token: ", unasked="pipe it in")


def test_the_cli_and_switchers_ask_only_through_prompts():
    """No module but prompts.py calls input() for a question."""
    import ast
    from pathlib import Path

    import ai_quota_swap

    root = Path(ai_quota_swap.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "prompts.py" or "tui" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "input":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
