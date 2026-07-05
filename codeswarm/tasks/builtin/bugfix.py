"""Builtin fix-the-bug tasks (difficulty=medium) for corpus diversity.

Unlike the stub tasks in ``__init__.py`` (whose starters ``raise
NotImplementedError``), each task here ships a FULLY WRITTEN function that
already runs but contains a SUBTLE BUG. The agent must find and fix the bug so
the pytest oracle passes. These produce real logic-error / wrong-output failure
signatures rather than plain NotImplementedError, which diversifies the corpus.

``reference_solution`` is MOCK-ONLY (used by the offline MockClient).
"""
from __future__ import annotations

from codeswarm.tasks.spec import Task

# --------------------------------------------------------------------------
# Task 1: fix_fizzbuzz — classic ordering bug (checks 3/5 before 15).
# --------------------------------------------------------------------------
_FIZZBUZZ_STARTER = '''\
"""fizzbuzz(n) -> list for 1..n. Contains a bug: fix it so the tests pass."""


def fizzbuzz(n):
    out = []
    for i in range(1, n + 1):
        if i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        elif i % 15 == 0:
            out.append("FizzBuzz")
        else:
            out.append(str(i))
    return out
'''

_FIZZBUZZ_TESTS = '''\
from fizzbuzz import fizzbuzz


def test_basic_values():
    result = fizzbuzz(5)
    assert result == ["1", "2", "Fizz", "4", "Buzz"]


def test_fizzbuzz_at_15():
    result = fizzbuzz(15)
    assert result[-1] == "FizzBuzz"
    assert result[2] == "Fizz"
    assert result[4] == "Buzz"


def test_length_and_thirty():
    result = fizzbuzz(30)
    assert len(result) == 30
    assert result[29] == "FizzBuzz"
    assert result[0] == "1"
'''

_FIZZBUZZ_SOLUTION = '''\
"""Reference implementation."""


def fizzbuzz(n):
    out = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    return out
'''

fix_fizzbuzz = Task(
    id="fix_fizzbuzz",
    prompt=(
        "fizzbuzz(n) in fizzbuzz.py should return a list for 1..n where each "
        "element is 'FizzBuzz' if divisible by 15, else 'Fizz' if divisible by "
        "3, else 'Buzz' if divisible by 5, else str(i). The starter has a bug: "
        "it checks divisibility by 3 and 5 before 15, so multiples of 15 wrongly "
        "yield 'Fizz'. Fix the ordering so test_fizzbuzz.py passes."
    ),
    files={"fizzbuzz.py": _FIZZBUZZ_STARTER},
    test_files={"test_fizzbuzz.py": _FIZZBUZZ_TESTS},
    reference_solution={"fizzbuzz.py": _FIZZBUZZ_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 2: fix_avg — integer division bug in the mean.
# --------------------------------------------------------------------------
_AVG_STARTER = '''\
"""average(nums) -> mean, or 0.0 for []. Contains a bug: fix it."""


def average(nums):
    if not nums:
        return 0.0
    return sum(nums) // len(nums)
'''

_AVG_TESTS = '''\
from stats import average


def test_simple_mean():
    assert average([1, 2]) == 1.5


def test_longer_mean():
    assert average([1, 2, 3, 4]) == 2.5


def test_empty_list():
    assert average([]) == 0.0


def test_whole_number():
    assert average([2, 4, 6]) == 4.0
'''

_AVG_SOLUTION = '''\
"""Reference implementation."""


def average(nums):
    if not nums:
        return 0.0
    return sum(nums) / len(nums)
'''

fix_avg = Task(
    id="fix_avg",
    prompt=(
        "average(nums) in stats.py should return the arithmetic mean of the "
        "numbers as a float, and return 0.0 for an empty list. The starter has a "
        "bug: it uses integer division (//), so average([1, 2]) returns 1 "
        "instead of 1.5. Fix it so test_stats.py passes."
    ),
    files={"stats.py": _AVG_STARTER},
    test_files={"test_stats.py": _AVG_TESTS},
    reference_solution={"stats.py": _AVG_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 3: fix_dedup — set() loses first-seen order.
# --------------------------------------------------------------------------
_DEDUP_STARTER = '''\
"""dedup(items) -> list with dups removed, order preserved. Fix the bug."""


def dedup(items):
    return list(set(items))
'''

_DEDUP_TESTS = '''\
from dedup import dedup


def test_preserves_order():
    assert dedup([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_no_duplicates():
    assert dedup([1, 2, 3]) == [1, 2, 3]


def test_strings_order():
    assert dedup(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_empty():
    assert dedup([]) == []
'''

_DEDUP_SOLUTION = '''\
"""Reference implementation."""


def dedup(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
'''

fix_dedup = Task(
    id="fix_dedup",
    prompt=(
        "dedup(items) in dedup.py should remove duplicate values while "
        "preserving the first-seen order of the remaining elements. The starter "
        "has a bug: it uses list(set(items)), which drops duplicates but loses "
        "the original order. Fix it so test_dedup.py passes."
    ),
    files={"dedup.py": _DEDUP_STARTER},
    test_files={"test_dedup.py": _DEDUP_TESTS},
    reference_solution={"dedup.py": _DEDUP_SOLUTION},
    difficulty="medium",
)


TASKS: list[Task] = [fix_fizzbuzz, fix_avg, fix_dedup]
