from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Self

from httpx2 import HTTPStatusError

from github_wikidata_bot.wikidata_api import APIError, MaxLagStats, ServerError


class ProjectOutcome(Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


class SkipReason(Enum):
    REPOSITORY_NOT_FOUND = "repository not found"
    REPOSITORY_UNAVAILABLE = "repository unavailable"
    MISSING_ENTITY = "missing entity"
    ENTITY_TOO_BIG = "entity too big"
    INVALID_PROJECT = "invalid project"
    SERVER_LAG = "server lag"
    TIMEOUT = "project timeout"
    TIMEOUT_DURING_MAXLAG = "project timeout during maxlag wait"
    RATE_LIMIT = "rate limit retries exhausted"
    DRY_RUN = "dry run"
    DUPLICATE_RELEASES = "duplicate releases"


@dataclass(frozen=True)
class FailureReason:
    error_type: type[Exception]
    code: int | str | None = None

    @classmethod
    def from_exception(cls, error: Exception) -> Self:
        if isinstance(error, HTTPStatusError):
            return cls(type(error), error.response.status_code)
        if isinstance(error, ServerError):
            return cls(type(error), error.status_code)
        if isinstance(error, APIError):
            return cls(type(error), error.code)
        return cls(type(error))

    def __str__(self) -> str:
        if self.code is not None:
            return f"{self.error_type.__name__} ({self.code})"
        return self.error_type.__name__


@dataclass(frozen=True)
class ProjectResult:
    """A project's final outcome, including any retries.

    A skipped or failed project may have made some edits before stopping.
    """

    outcome: ProjectOutcome
    reason: SkipReason | FailureReason | None = None
    maxlag: MaxLagStats = field(default_factory=MaxLagStats)


@dataclass
class RunSummary:
    outcomes: Counter[ProjectOutcome] = field(default_factory=Counter)
    skips: Counter[SkipReason] = field(default_factory=Counter)
    failures: Counter[FailureReason] = field(default_factory=Counter)
    maxlag_projects: int = 0
    maxlag: MaxLagStats = field(default_factory=MaxLagStats)

    def record(self, result: ProjectResult) -> None:
        self.outcomes[result.outcome] += 1
        if result.maxlag.rejections:
            self.maxlag_projects += 1
        self.maxlag.rejections += result.maxlag.rejections
        self.maxlag.requested_wait_seconds += result.maxlag.requested_wait_seconds
        self.maxlag.wait_seconds += result.maxlag.wait_seconds
        if isinstance(result.reason, SkipReason):
            self.skips[result.reason] += 1
        elif isinstance(result.reason, FailureReason):
            self.failures[result.reason] += 1

    def __str__(self) -> str:
        totals = ", ".join(
            f"{outcome.value}={self.outcomes[outcome]}" for outcome in ProjectOutcome
        )
        details = [totals]
        if self.skips:
            details.append(
                "skip reasons: "
                + ", ".join(
                    f"{reason.value}={count}"
                    for reason, count in sorted(
                        self.skips.items(), key=lambda entry: entry[0].value
                    )
                )
            )
        if self.failures:
            details.append(
                "failure reasons: "
                + ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(
                        self.failures.items(), key=lambda entry: str(entry[0])
                    )
                )
            )
        details.append(f"maxlag: projects={self.maxlag_projects}, {self.maxlag}")
        return "; ".join(details)
