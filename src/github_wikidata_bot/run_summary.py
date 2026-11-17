from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from httpx2 import HTTPStatusError

from github_wikidata_bot.wikidata_api import (
    APIError,
    MaxLagStats,
    MaxLagWaitCancelledError,
    ServerError,
)


class ProjectOutcome(Enum):
    UP_TO_DATE = "up-to-date"
    UPDATED = "updated"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class SkipReason(Enum):
    INVALID_PROJECT = "invalid project"
    DRY_RUN = "dry run"
    DUPLICATE_RELEASES = "duplicate releases"
    NO_RELEASES_OR_TAGS = "no GitHub releases or tags"
    PRERELEASES_ONLY = "only prereleases"
    NO_USABLE_STABLE_RELEASE = "no usable stable release"
    PREFERRED_VERSION_NOT_ON_GITHUB = (
        "preferred Wikidata version absent from GitHub results"
    )


class UnavailableReason(Enum):
    REPOSITORY_NOT_FOUND = "repository not found"
    REPOSITORY_UNAVAILABLE = "repository unavailable"
    MISSING_ENTITY = "missing entity"
    ENTITY_TOO_BIG = "entity too big"


@dataclass(frozen=True)
class FailureReason:
    exception_type: type[Exception]
    http_status: int | None = None
    api_code: str | None = None
    timed_out_during_maxlag: bool = False

    @classmethod
    def from_exception(cls, error: Exception) -> FailureReason:
        if isinstance(error, HTTPStatusError):
            return cls(type(error), http_status=error.response.status_code)
        if isinstance(error, ServerError):
            return cls(type(error), http_status=error.status_code)
        if isinstance(error, APIError):
            return cls(type(error), api_code=error.code)
        # asyncio.wait_for preserves the interrupted wait as the timeout's cause.
        if isinstance(error, TimeoutError) and isinstance(
            error.__cause__, MaxLagWaitCancelledError
        ):
            return cls(type(error), timed_out_during_maxlag=True)
        return cls(type(error))

    def __str__(self) -> str:
        name = self.exception_type.__name__
        if self.http_status is not None:
            return f"{name} ({self.http_status})"
        if self.api_code is not None:
            return f"{name} ({self.api_code})"
        if self.timed_out_during_maxlag:
            return f"{name} (maxlag wait)"
        return name


@dataclass(frozen=True)
class ProjectResult:
    """A project's final outcome, including any retries.

    A project may have made some edits before stopping early.
    """

    outcome: ProjectOutcome
    reason: SkipReason | UnavailableReason | FailureReason | None = None
    maxlag: MaxLagStats = field(default_factory=MaxLagStats)

    def __str__(self) -> str:
        reason = self.reason
        if reason is None:
            return self.outcome.value
        if isinstance(reason, (SkipReason, UnavailableReason)):
            reason = reason.value
        if self.outcome is ProjectOutcome.UPDATED:
            return f"updated (latest version left unchanged: {reason})"
        return f"{self.outcome.value} ({reason})"


@dataclass
class RunSummary:
    outcomes: Counter[ProjectOutcome] = field(default_factory=Counter)
    skips: Counter[SkipReason] = field(default_factory=Counter)
    unavailable: Counter[UnavailableReason] = field(default_factory=Counter)
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
        if result.outcome is ProjectOutcome.SKIPPED and isinstance(
            result.reason, SkipReason
        ):
            self.skips[result.reason] += 1
        elif isinstance(result.reason, UnavailableReason):
            self.unavailable[result.reason] += 1
        elif isinstance(result.reason, FailureReason):
            self.failures[result.reason] += 1

    def __str__(self) -> str:
        totals = ", ".join(
            f"{outcome.value}={self.outcomes[outcome]}" for outcome in ProjectOutcome
        )
        details = [totals]
        for heading, reasons in (
            ("skip reasons", self.skips),
            ("unavailable reasons", self.unavailable),
        ):
            if not reasons:
                continue
            details.append(
                f"{heading}: "
                + ", ".join(
                    f"{reason.value}={count}"
                    for reason, count in sorted(
                        reasons.items(), key=lambda entry: entry[0].value
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
