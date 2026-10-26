"""Messages the crew passes along the workflow graph, and the structured replies agents return."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


class Claim(BaseModel):
    """A checkable fact inside a line. The Verifier resolves every claim against the match record."""
    kind: Literal["score", "team_stat", "player_stat", "event", "explain", "leader"]
    team: Literal["home", "away"] | None = None
    player: str | None = None
    stat: str | None = None
    id: int | None = None
    field: str | None = None
    home: int | None = None
    away: int | None = None
    value: float | str | int | None = None


class Insight(BaseModel):
    """The Analyst's read of a moment."""
    headline: str = Field(description="six words at most, plain")
    why_it_matters: str = Field(description="one or two sentences: the football reason this moment counts")
    pattern: Literal["control", "chaos", "pressure", "rhythm", "individual", "set_piece", "game_state"]
    claims: list[Claim] = Field(default_factory=list)
    sources: list[int] = Field(default_factory=list)


class PersonaLine(BaseModel):
    persona: Literal["analyst", "casual", "studio"]
    text: str
    claims: list[Claim] = Field(default_factory=list)
    sources: list[int] = Field(default_factory=list)


class Lines(BaseModel):
    lines: list[PersonaLine]


class Translation(BaseModel):
    lang: str
    text: str


class Translations(BaseModel):
    items: list[Translation]


class Recap(BaseModel):
    title: str
    story: str = Field(description="four to six sentences")
    key_moments: list[str]
    claims: list[Claim] = Field(default_factory=list)
    sources: list[int] = Field(default_factory=list)


@dataclass
class Job:
    """One moment travelling through the crew, accumulating each agent's work."""
    match_id: str
    kind: str
    match_s: float
    frame: int
    period: int
    priority: int
    facts: dict
    sources: list[int]
    context: dict
    personas: list[str]
    langs: list[str]
    team: str | None = None
    player: str | None = None
    roster: list[str] = field(default_factory=list)
    clubs: list[str] = field(default_factory=list)
    insight: Insight | None = None
    insight_ok: bool | None = None                     # None until the Verifier has checked the insight
    lines: list[PersonaLine] = field(default_factory=list)
    recap: Recap | None = None
    verdicts: list[dict] = field(default_factory=list)
    attempts: int = 0
    fallback: bool = False
    translations: dict = field(default_factory=dict)
    audio: dict = field(default_factory=dict)          # persona -> {lang: url}, from the Voice step
    trace: list[dict] = field(default_factory=list)
