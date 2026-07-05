"""Builtin medium-difficulty algorithm tasks with pytest oracles.

Each ships starter files (deliberately failing stubs), test files (the oracle),
and a reference_solution used ONLY by the offline MockClient. Same contract as
the tasks in ``__init__.py``; exported here as the module-level ``TASKS`` list.
"""
from __future__ import annotations

from codeswarm.tasks.spec import Task

# --------------------------------------------------------------------------
# Task 1: binary_search — implement binary_search(items, target).
# --------------------------------------------------------------------------
_BSEARCH_STARTER = '''\
"""Implement the function so the tests pass."""


def binary_search(items, target):
    raise NotImplementedError
'''

_BSEARCH_TESTS = '''\
from binary_search import binary_search


def test_found_in_middle():
    assert binary_search([1, 3, 5, 7, 9], 5) == 2


def test_found_at_ends():
    assert binary_search([1, 3, 5, 7, 9], 1) == 0
    assert binary_search([1, 3, 5, 7, 9], 9) == 4


def test_not_present():
    assert binary_search([1, 3, 5, 7, 9], 4) == -1
    assert binary_search([1, 3, 5, 7, 9], 0) == -1
    assert binary_search([1, 3, 5, 7, 9], 10) == -1


def test_empty_list():
    assert binary_search([], 1) == -1


def test_single_element():
    assert binary_search([42], 42) == 0
    assert binary_search([42], 7) == -1
'''

_BSEARCH_SOLUTION = '''\
"""Reference implementation."""


def binary_search(items, target):
    lo, hi = 0, len(items) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if items[mid] == target:
            return mid
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
'''

binary_search_task = Task(
    id="binary_search",
    prompt=(
        "Implement `binary_search(items, target)` in binary_search.py so that "
        "test_binary_search.py passes. `items` is a list sorted in ascending "
        "order; return the index of `target` if present, otherwise -1. Use an "
        "efficient O(log n) binary search."
    ),
    files={"binary_search.py": _BSEARCH_STARTER},
    test_files={"test_binary_search.py": _BSEARCH_TESTS},
    reference_solution={"binary_search.py": _BSEARCH_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 2: run_length — implement encode(s) and decode(s).
# --------------------------------------------------------------------------
_RLE_STARTER = '''\
"""Implement the two functions so the tests pass."""


def encode(s):
    raise NotImplementedError


def decode(s):
    raise NotImplementedError
'''

_RLE_TESTS = '''\
from run_length import encode, decode


def test_encode_basic():
    assert encode("aaabb") == "a3b2"
    assert encode("wwwwww") == "w6"


def test_decode_basic():
    assert decode("a3b2") == "aaabb"
    assert decode("w6") == "wwwwww"


def test_round_trip():
    for s in ("aaabb", "abcabc", "zzzzzzzzzz", "mississippi"):
        assert decode(encode(s)) == s


def test_empty():
    assert encode("") == ""
    assert decode("") == ""


def test_single_chars():
    assert encode("abc") == "a1b1c1"
    assert decode("a1b1c1") == "abc"
    assert encode("a") == "a1"
    assert decode("a1") == "a"
'''

_RLE_SOLUTION = '''\
"""Reference implementation."""


def encode(s):
    if not s:
        return ""
    out = []
    prev = s[0]
    count = 1
    for ch in s[1:]:
        if ch == prev:
            count += 1
        else:
            out.append(prev + str(count))
            prev = ch
            count = 1
    out.append(prev + str(count))
    return "".join(out)


def decode(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        i += 1
        j = i
        while j < n and s[j].isdigit():
            j += 1
        count = int(s[i:j])
        out.append(ch * count)
        i = j
    return "".join(out)
'''

run_length_task = Task(
    id="run_length",
    prompt=(
        "Implement `encode(s)` and `decode(s)` in run_length.py so that "
        "test_run_length.py passes. `encode` performs run-length encoding, "
        'emitting each character followed by its run count (e.g. encode("aaabb") '
        '== "a3b2"); `decode` is its inverse. `encode("")` and `decode("")` both '
        "return the empty string, and decode(encode(s)) must round-trip."
    ),
    files={"run_length.py": _RLE_STARTER},
    test_files={"test_run_length.py": _RLE_TESTS},
    reference_solution={"run_length.py": _RLE_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 3: matrix_ops — implement transpose(m) and row_sums(m).
# --------------------------------------------------------------------------
_MATRIX_STARTER = '''\
"""Implement the two functions so the tests pass."""


def transpose(m):
    raise NotImplementedError


def row_sums(m):
    raise NotImplementedError
'''

_MATRIX_TESTS = '''\
from matrix_ops import transpose, row_sums


def test_transpose_non_square():
    assert transpose([[1, 2, 3], [4, 5, 6]]) == [[1, 4], [2, 5], [3, 6]]


def test_transpose_single_row():
    assert transpose([[1, 2, 3]]) == [[1], [2], [3]]


def test_transpose_empty():
    assert transpose([]) == []


def test_row_sums_non_square():
    assert row_sums([[1, 2, 3], [4, 5, 6]]) == [6, 15]


def test_row_sums_single_row():
    assert row_sums([[1, 2, 3]]) == [6]


def test_row_sums_empty():
    assert row_sums([]) == []
'''

_MATRIX_SOLUTION = '''\
"""Reference implementation."""


def transpose(m):
    if not m:
        return []
    return [list(col) for col in zip(*m)]


def row_sums(m):
    return [sum(row) for row in m]
'''

matrix_ops_task = Task(
    id="matrix_ops",
    prompt=(
        "Implement `transpose(m)` and `row_sums(m)` in matrix_ops.py so that "
        "test_matrix_ops.py passes. `m` is a matrix represented as a list of "
        "row lists. `transpose` returns the matrix with rows and columns "
        "swapped (an empty matrix transposes to an empty list); `row_sums` "
        "returns a list with the sum of each row."
    ),
    files={"matrix_ops.py": _MATRIX_STARTER},
    test_files={"test_matrix_ops.py": _MATRIX_TESTS},
    reference_solution={"matrix_ops.py": _MATRIX_SOLUTION},
    difficulty="medium",
)


TASKS: list[Task] = [binary_search_task, run_length_task, matrix_ops_task]
