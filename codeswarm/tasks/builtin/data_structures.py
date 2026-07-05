"""Builtin data-structure tasks that exercise STATE/mutation failure modes.

Each ships starter files (deliberately failing stubs), test files (the oracle),
and a reference_solution used ONLY by the offline MockClient. They stress
stateful behaviour: LIFO ordering, LRU eviction/recency, and accumulating
counts — where mutation bugs (wrong order, missed eviction, off-by-one) surface.
"""
from __future__ import annotations

from codeswarm.tasks.spec import Task

# --------------------------------------------------------------------------
# Task 1: stack — implement a LIFO Stack class.
# --------------------------------------------------------------------------
_STACK_STARTER = '''\
"""Implement the Stack class so the tests pass."""


class Stack:
    def __init__(self):
        raise NotImplementedError

    def push(self, x):
        raise NotImplementedError

    def pop(self):
        raise NotImplementedError

    def peek(self):
        raise NotImplementedError

    def is_empty(self):
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError
'''

_STACK_TESTS = '''\
import pytest

from stack import Stack


def test_lifo_order():
    s = Stack()
    s.push(1)
    s.push(2)
    s.push(3)
    assert s.pop() == 3
    assert s.pop() == 2
    assert s.pop() == 1


def test_peek_does_not_remove():
    s = Stack()
    s.push("a")
    s.push("b")
    assert s.peek() == "b"
    assert len(s) == 2
    assert s.pop() == "b"
    assert s.peek() == "a"


def test_is_empty_and_len():
    s = Stack()
    assert s.is_empty() is True
    assert len(s) == 0
    s.push(10)
    assert s.is_empty() is False
    assert len(s) == 1
    s.pop()
    assert s.is_empty() is True
    assert len(s) == 0


def test_pop_empty_raises():
    s = Stack()
    with pytest.raises(IndexError):
        s.pop()


def test_peek_empty_raises():
    s = Stack()
    with pytest.raises(IndexError):
        s.peek()
'''

_STACK_SOLUTION = '''\
"""Reference implementation."""


class Stack:
    def __init__(self):
        self._items = []

    def push(self, x):
        self._items.append(x)

    def pop(self):
        if not self._items:
            raise IndexError("pop from empty stack")
        return self._items.pop()

    def peek(self):
        if not self._items:
            raise IndexError("peek from empty stack")
        return self._items[-1]

    def is_empty(self):
        return len(self._items) == 0

    def __len__(self):
        return len(self._items)
'''

stack = Task(
    id="stack",
    prompt=(
        "Implement a `Stack` class in stack.py backed by a LIFO container. "
        "It must support push(x), pop() (return and remove the top item, raising "
        'IndexError("pop from empty stack") when empty), peek() (return the top '
        "item without removing it, raising IndexError when empty), is_empty(), and "
        "__len__ so that test_stack.py passes."
    ),
    files={"stack.py": _STACK_STARTER},
    test_files={"test_stack.py": _STACK_TESTS},
    reference_solution={"stack.py": _STACK_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 2: lru_cache — implement a capacity-bounded LRU cache.
# --------------------------------------------------------------------------
_LRU_STARTER = '''\
"""Implement the LRUCache class so the tests pass."""


class LRUCache:
    def __init__(self, capacity):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError
'''

_LRU_TESTS = '''\
from lru_cache import LRUCache


def test_get_missing_returns_none():
    c = LRUCache(2)
    assert c.get("nope") is None


def test_basic_put_get():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1
    assert c.get("b") == 2


def test_eviction_order():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)  # evicts "a" (least recently used)
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_get_affects_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # "a" is now most recently used
    c.put("c", 3)           # evicts "b", not "a"
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3


def test_update_existing_key():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 10)  # update, also refreshes recency
    c.put("c", 3)   # evicts "b", not "a"
    assert c.get("a") == 10
    assert c.get("b") is None
    assert c.get("c") == 3


def test_capacity_one():
    c = LRUCache(1)
    c.put("a", 1)
    assert c.get("a") == 1
    c.put("b", 2)  # evicts "a"
    assert c.get("a") is None
    assert c.get("b") == 2
'''

_LRU_SOLUTION = '''\
"""Reference implementation."""

from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self._store = OrderedDict()

    def get(self, key):
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key, value):
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self.capacity:
            self._store.popitem(last=False)
'''

lru_cache = Task(
    id="lru_cache",
    prompt=(
        "Implement an `LRUCache` class in lru_cache.py. __init__(capacity) sets the "
        "maximum number of entries. get(key) returns the value or None if absent and "
        "marks the key as most recently used. put(key, value) inserts or updates a key "
        "(also marking it most recently used) and evicts the least-recently-used entry "
        "when the cache exceeds its capacity, so that test_lru_cache.py passes."
    ),
    files={"lru_cache.py": _LRU_STARTER},
    test_files={"test_lru_cache.py": _LRU_TESTS},
    reference_solution={"lru_cache.py": _LRU_SOLUTION},
    difficulty="medium",
)


# --------------------------------------------------------------------------
# Task 3: word_count — count words case-insensitively, stripping punctuation.
# --------------------------------------------------------------------------
_WC_STARTER = '''\
"""Implement word_count so the tests pass."""


def word_count(text):
    raise NotImplementedError
'''

_WC_TESTS = '''\
from word_count import word_count


def test_empty_string():
    assert word_count("") == {}
    assert word_count("   ") == {}


def test_repeated_words():
    assert word_count("the cat the dog the bird") == {
        "the": 3,
        "cat": 1,
        "dog": 1,
        "bird": 1,
    }


def test_case_insensitive():
    assert word_count("Hello hello HELLO") == {"hello": 3}


def test_strips_surrounding_punctuation():
    assert word_count("Hello, world! Hello.") == {"hello": 2, "world": 1}


def test_mixed():
    assert word_count("Yes? Yes; no. NO!") == {"yes": 2, "no": 2}
'''

_WC_SOLUTION = '''\
"""Reference implementation."""


def word_count(text):
    counts = {}
    for token in text.split():
        word = token.strip(".,!?;").lower()
        if not word:
            continue
        counts[word] = counts.get(word, 0) + 1
    return counts
'''

word_count = Task(
    id="word_count",
    prompt=(
        "Implement `word_count(text)` in word_count.py returning a dict that maps each "
        "lowercase word to its number of occurrences. Split the text on whitespace, "
        "strip surrounding punctuation (. , ! ? ;) from each token, lowercase it, and "
        "ignore empty tokens so that an empty string yields {} and test_word_count.py "
        "passes."
    ),
    files={"word_count.py": _WC_STARTER},
    test_files={"test_word_count.py": _WC_TESTS},
    reference_solution={"word_count.py": _WC_SOLUTION},
    difficulty="medium",
)


TASKS: list[Task] = [stack, lru_cache, word_count]
