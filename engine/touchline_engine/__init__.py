"""Touchline match engine: synthetic, football-realistic matches with events and 25 Hz tracking."""

from .sim import Match
from .squads import make_fixture

__all__ = ["Match", "make_fixture", "generate"]


def generate(seed: int):
    """Play one full match for a seed and return the finished Match."""
    return Match(seed).play()
