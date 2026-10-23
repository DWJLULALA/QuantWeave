"""Frozen geometry exposed to the pinned feature-sequence implementation."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from .enums import BI_DIR


@dataclass
class Line:
    idx: int
    rank: int
    start_idx: int
    end_idx: int
    start_price: float
    end_price: float
    available_idx: int
    child_start: int = -1
    child_end: int = -1
    evidence: dict = field(default_factory=dict)
    next: Optional['Line'] = field(default=None, repr=False)
    pre: Optional['Line'] = field(default=None, repr=False)

    @property
    def dir(self): return BI_DIR.UP if self.end_price > self.start_price else BI_DIR.DOWN
    @property
    def is_used_to_be_sure(self): return True
    @property
    def is_sure(self): return True
    def is_up(self): return self.dir == BI_DIR.UP
    def is_down(self): return self.dir == BI_DIR.DOWN
    def _high(self): return max(self.start_price, self.end_price)
    def _low(self): return min(self.start_price, self.end_price)
    def get_end_val(self): return self.end_price
    def get_begin_val(self): return self.start_price


class UnusedKline:
    """The upstream combiner's K-line-only mutation path is never used."""


def revert_bi_dir(direction):
    return BI_DIR.DOWN if direction == BI_DIR.UP else BI_DIR.UP
