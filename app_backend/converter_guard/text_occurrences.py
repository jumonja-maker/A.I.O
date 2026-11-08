"""One bounded non-overlapping text allocator for host and embedded UNO checks.

The same trusted stdlib-only source is embedded in LibreOffice's worker.
Callers retain their existing normalization, diagnostics and cancellation.
"""

TEXT_OCCURRENCE_SOURCE = r'''
from bisect import bisect_left
from collections import Counter
import re


class AioTextAllocationIncomplete(ValueError):
    pass


class AioTextAllocationOverlap(ValueError):
    pass


def aio_allocate_text_occurrences(expected, whole, *, word_boundaries=True,
        cancel=None, match_work=64 * 1024 * 1024, allocation_work=64 * 1024,
        max_occurrences=20000, max_states=4096):
    """Return missing values, or prove a disjoint allocation of every value.

    A failed greedy choice never proves loss. Bounded exact search decides
    overlap, or reports incomplete instead of rejecting an unexamined case.
    Native callers may retain their historical whitespace-free matching by
    disabling word boundaries; the allocation still cannot reuse characters.
    """
    expected = Counter(expected)
    if any(not isinstance(value, str) or not value or type(count) is not int or count < 1
           for value, count in expected.items()):
        raise AioTextAllocationIncomplete('text_allocation_metadata')
    if len(whole) * len(expected) > match_work:
        raise AioTextAllocationIncomplete('text_matching_budget')
    patterns, missing = [], Counter()
    for value, required in expected.items():
        pattern = re.escape(value)
        if word_boundaries:
            pattern = r'(?<!\w)' + pattern + r'(?!\w)'
        pattern = re.compile(pattern)
        patterns.append(pattern)
        observed = sum(1 for _ in pattern.finditer(whole))
        if observed < required:
            missing[value] = required - observed
        if cancel is not None:
            cancel()
    if missing or len(patterns) < 2:
        return missing

    required = tuple(expected.values())
    end = 0
    for pattern, count in zip(patterns, required):
        if cancel is not None:
            cancel()
        for _ in range(count):
            match = pattern.search(whole, end)
            if match is None:
                break
            end = match.end()
        else:
            continue
        break
    else:
        return missing

    work = 0
    def charge(amount):
        nonlocal work
        work += amount
        if work > allocation_work:
            raise AioTextAllocationIncomplete('text_allocation_budget')
        if cancel is not None:
            cancel()

    charge(len(patterns))
    starts, intervals = [], []
    for index, pattern in enumerate(patterns):
        positions, offset = [], 0
        while match := pattern.search(whole, offset):
            charge(1)
            if len(intervals) >= max_occurrences:
                raise AioTextAllocationIncomplete('text_occurrence_budget')
            positions.append(match.start())
            intervals.append((match.end(), match.start(), index))
            offset = match.start() + 1
        starts.append(positions)

    # Earliest-end selection is a cheap witness only. Its failure is not a
    # rejection: a different choice may leave enough space for every value.
    remaining, end = list(required), 0
    for finish, start, index in sorted(intervals):
        if remaining[index] and start >= end:
            remaining[index] -= 1
            end = finish
    if not any(remaining):
        return missing

    # At equal fulfilled counts, the earliest end dominates every later end.
    states = {tuple(0 for _ in required): 0}
    lengths = tuple(len(value) for value in expected)
    for _ in range(sum(required)):
        following = {}
        for counts, end in states.items():
            charge(len(patterns))
            for index, count in enumerate(counts):
                if count >= required[index]:
                    continue
                position = bisect_left(starts[index], end)
                if position == len(starts[index]):
                    continue
                charge(len(patterns))
                advanced = list(counts)
                advanced[index] += 1
                advanced = tuple(advanced)
                if advanced == required:
                    return missing
                finish = starts[index][position] + lengths[index]
                if finish < following.get(advanced, float('inf')):
                    following[advanced] = finish
                if len(following) > max_states:
                    raise AioTextAllocationIncomplete('text_allocation_state_budget')
        states = following
        if not states:
            break
    raise AioTextAllocationOverlap('text_overlapping_occurrences')
'''

exec(compile(TEXT_OCCURRENCE_SOURCE, '<aio_text_occurrences>', 'exec'))
