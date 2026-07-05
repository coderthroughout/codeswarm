"""Builtin, self-contained Python tasks with pytest oracles.

Each ships starter files (deliberately failing stubs), test files (the oracle),
and a reference_solution used ONLY by the offline MockClient. They are designed
to fail in diverse ways so the corpus has signal.
"""
from __future__ import annotations

from codeswarm.tasks.spec import Task

# --------------------------------------------------------------------------
# Task 1: math_utils — implement add() and multiply().
# --------------------------------------------------------------------------
_MATH_STARTER = '''\
"""Implement the two functions so the tests pass."""


def add(a, b):
    raise NotImplementedError


def multiply(a, b):
    raise NotImplementedError
'''

_MATH_TESTS = '''\
from math_utils import add, multiply


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_multiply():
    assert multiply(2, 3) == 6
    assert multiply(0, 99) == 0
'''

_MATH_SOLUTION = '''\
"""Reference implementation."""


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
'''

math_utils = Task(
    id="math_utils",
    prompt=(
        "Implement `add(a, b)` and `multiply(a, b)` in math_utils.py so that "
        "test_math_utils.py passes. `add` returns the sum; `multiply` returns "
        "the product."
    ),
    files={"math_utils.py": _MATH_STARTER},
    test_files={"test_math_utils.py": _MATH_TESTS},
    reference_solution={"math_utils.py": _MATH_SOLUTION},
    difficulty="easy",
)


# --------------------------------------------------------------------------
# Task 2: strings — implement is_palindrome() and reverse_words().
# --------------------------------------------------------------------------
_STR_STARTER = '''\
"""Implement the two functions so the tests pass."""


def is_palindrome(s):
    raise NotImplementedError


def reverse_words(s):
    raise NotImplementedError
'''

_STR_TESTS = '''\
from strutils import is_palindrome, reverse_words


def test_is_palindrome():
    assert is_palindrome("racecar") is True
    assert is_palindrome("hello") is False
    assert is_palindrome("") is True


def test_reverse_words():
    assert reverse_words("hello world") == "world hello"
    assert reverse_words("a b c") == "c b a"
'''

_STR_SOLUTION = '''\
"""Reference implementation."""


def is_palindrome(s):
    return s == s[::-1]


def reverse_words(s):
    return " ".join(reversed(s.split()))
'''

strutils = Task(
    id="strutils",
    prompt=(
        "Implement `is_palindrome(s)` (True iff s reads the same reversed) and "
        "`reverse_words(s)` (reverse the order of whitespace-separated words) in "
        "strutils.py so test_strutils.py passes."
    ),
    files={"strutils.py": _STR_STARTER},
    test_files={"test_strutils.py": _STR_TESTS},
    reference_solution={"strutils.py": _STR_SOLUTION},
    difficulty="easy",
)


# Task modules authored alongside this one. Each exports a module-level
# ``TASKS: list[Task]``. Import them here so they register in BUILTIN_TASKS.
from codeswarm.tasks.builtin.algorithms import TASKS as _ALGO_TASKS
from codeswarm.tasks.builtin.bugfix import TASKS as _BUGFIX_TASKS
from codeswarm.tasks.builtin.data_structures import TASKS as _DS_TASKS
from codeswarm.tasks.builtin.parsing import TASKS as _PARSING_TASKS

_ALL_TASKS: list[Task] = [
    math_utils,
    strutils,
    *_ALGO_TASKS,
    *_DS_TASKS,
    *_PARSING_TASKS,
    *_BUGFIX_TASKS,
]

BUILTIN_TASKS: dict[str, Task] = {t.id: t for t in _ALL_TASKS}

# Fail loud if two modules ever collide on an id (would silently drop a task).
assert len(BUILTIN_TASKS) == len(_ALL_TASKS), "duplicate builtin task id detected"


def list_tasks() -> list[str]:
    """Return the sorted ids of all builtin tasks."""
    return sorted(BUILTIN_TASKS)


def get_task(task_id: str) -> Task:
    """Look up a builtin task by id (raises KeyError if unknown)."""
    return BUILTIN_TASKS[task_id]
