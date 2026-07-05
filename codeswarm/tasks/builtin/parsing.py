"""Builtin parsing tasks with pytest oracles (medium difficulty).

Each task exercises string parsing and edge-case failure modes. They ship
starter files (deliberately failing stubs), test files (the oracle), and a
reference_solution used ONLY by the offline MockClient.
"""
from __future__ import annotations

from codeswarm.tasks.spec import Task

# --------------------------------------------------------------------------
# Task 1: parse_kv — parse "a=1,b=2,c=hello" into {"a":"1","b":"2","c":"hello"}.
# --------------------------------------------------------------------------
_KV_STARTER = '''\
"""Implement parse_kv so the tests pass."""


def parse_kv(s):
    raise NotImplementedError
'''

_KV_TESTS = '''\
from parse_kv import parse_kv


def test_basic():
    assert parse_kv("a=1,b=2,c=hello") == {"a": "1", "b": "2", "c": "hello"}


def test_whitespace():
    assert parse_kv("a = 1 , b = 2") == {"a": "1", "b": "2"}


def test_empty():
    assert parse_kv("") == {}


def test_single_pair():
    assert parse_kv("key=value") == {"key": "value"}
'''

_KV_SOLUTION = '''\
"""Reference implementation."""


def parse_kv(s):
    result = {}
    if s.strip() == "":
        return result
    for pair in s.split(","):
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result
'''

parse_kv = Task(
    id="parse_kv",
    prompt=(
        "Implement `parse_kv(s)` in parse_kv.py so test_parse_kv.py passes. "
        "Parse a comma-separated string of key=value pairs into a dict, keeping "
        "values as strings, e.g. \"a=1,b=2,c=hello\" -> {\"a\": \"1\", \"b\": \"2\", "
        "\"c\": \"hello\"}. An empty string returns {}, and surrounding whitespace "
        "must be tolerated so \"a = 1 , b = 2\" -> {\"a\": \"1\", \"b\": \"2\"}."
    ),
    files={"parse_kv.py": _KV_STARTER},
    test_files={"test_parse_kv.py": _KV_TESTS},
    reference_solution={"parse_kv.py": _KV_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 2: roman — implement to_roman(n) and from_roman(s) for 1..3999.
# --------------------------------------------------------------------------
_ROMAN_STARTER = '''\
"""Implement the two functions so the tests pass."""


def to_roman(n):
    raise NotImplementedError


def from_roman(s):
    raise NotImplementedError
'''

_ROMAN_TESTS = '''\
from roman import to_roman, from_roman


CASES = [
    (1, "I"),
    (4, "IV"),
    (9, "IX"),
    (14, "XIV"),
    (40, "XL"),
    (90, "XC"),
    (400, "CD"),
    (900, "CM"),
    (1994, "MCMXCIV"),
    (3999, "MMMCMXCIX"),
]


def test_to_roman():
    for n, s in CASES:
        assert to_roman(n) == s


def test_from_roman():
    for n, s in CASES:
        assert from_roman(s) == n


def test_round_trip():
    for n in (1, 4, 9, 14, 40, 90, 400, 900, 1994, 3999):
        assert from_roman(to_roman(n)) == n
'''

_ROMAN_SOLUTION = '''\
"""Reference implementation."""

_VALUES = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]

_SYMBOLS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_roman(n):
    out = []
    for value, symbol in _VALUES:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def from_roman(s):
    total = 0
    prev = 0
    for ch in reversed(s):
        value = _SYMBOLS[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total
'''

roman = Task(
    id="roman",
    prompt=(
        "Implement `to_roman(n)` and `from_roman(s)` in roman.py so test_roman.py "
        "passes. Convert between integers in the range 1..3999 and Roman numerals "
        "using standard subtractive notation (4=IV, 9=IX, 40=XL, 90=XC, 400=CD, "
        "900=CM), e.g. to_roman(1994) == \"MCMXCIV\" and from_roman(\"MCMXCIV\") == "
        "1994. The two functions must be exact inverses."
    ),
    files={"roman.py": _ROMAN_STARTER},
    test_files={"test_roman.py": _ROMAN_TESTS},
    reference_solution={"roman.py": _ROMAN_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 3: ini_parse — parse simple INI text into {section: {key: value}}.
# --------------------------------------------------------------------------
_INI_STARTER = '''\
"""Implement parse_ini so the tests pass."""


def parse_ini(text):
    raise NotImplementedError
'''

_INI_TESTS = '''\
from ini_parse import parse_ini


SAMPLE = """
# a comment
[server]
host = localhost
port = 8080

; another comment
[db]
name = omium
"""


def test_sections():
    result = parse_ini(SAMPLE)
    assert set(result) == {"server", "db"}


def test_values_and_trimming():
    result = parse_ini(SAMPLE)
    assert result["server"] == {"host": "localhost", "port": "8080"}
    assert result["db"] == {"name": "omium"}


def test_comments_and_blank_lines_ignored():
    text = "[s]\\n\\n# c\\n; c2\\nk = v\\n"
    assert parse_ini(text) == {"s": {"k": "v"}}


def test_empty():
    assert parse_ini("") == {}
'''

_INI_SOLUTION = '''\
"""Reference implementation."""


def parse_ini(text):
    result = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            result.setdefault(current, {})
            continue
        key, sep, value = line.partition("=")
        if sep and current is not None:
            result[current][key.strip()] = value.strip()
    return result
'''

ini_parse = Task(
    id="ini_parse",
    prompt=(
        "Implement `parse_ini(text)` in ini_parse.py so test_ini_parse.py passes. "
        "Parse simple INI text into a nested dict {section: {key: value}}: a line "
        "like \"[server]\" starts a section, and \"key = value\" adds an entry to "
        "the current section (keys and values trimmed of whitespace). Blank lines "
        "and lines starting with \"#\" or \";\" are ignored, e.g. "
        "\"[s]\\nk = v\" -> {\"s\": {\"k\": \"v\"}}."
    ),
    files={"ini_parse.py": _INI_STARTER},
    test_files={"test_ini_parse.py": _INI_TESTS},
    reference_solution={"ini_parse.py": _INI_SOLUTION},
    difficulty="medium",
)


TASKS: list[Task] = [parse_kv, roman, ini_parse]
