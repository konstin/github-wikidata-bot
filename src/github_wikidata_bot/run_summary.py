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
class ExceptionFailure:
    exception_type: type[Exception]

    def __str__(self) -> str:
        return self.exception_type.__name__


@dataclass(frozen=True)
class HTTPStatusFailure:
    exception_type: type[HTTPStatusError | ServerError]
    status_code: int

    def __str__(self) -> str:
        return f"{self.exception_type.__name__} ({self.status_code})"


@dataclass(frozen=True)
class WikidataAPIFailure:
    exception_type: type[APIError]
    api_code: str

    def __str__(self) -> str:
        return f"{self.exception_type.__name__} ({self.api_code})"


@dataclass(frozen=True)
class MaxLagTimeoutFailure:
    exception_type: type[TimeoutError]

    def __str__(self) -> str:
        return f"{self.exception_type.__name__} (maxlag wait)"


type FailureReason = (
    ExceptionFailure | HTTPStatusFailure | WikidataAPIFailure | MaxLagTimeoutFailure
)


def classify_failure(error: Exception) -> FailureReason:
    if isinstance(error, HTTPStatusError):
        return HTTPStatusFailure(type(error), error.response.status_code)
    if isinstance(error, ServerError):
        return HTTPStatusFailure(type(error), error.status_code)
    if isinstance(error, APIError) and error.code is not None:
        return WikidataAPIFailure(type(error), error.code)
    # asyncio.wait_for preserves the interrupted wait as the timeout's cause.
    if isinstance(error, TimeoutError) and isinstance(
        error.__cause__, MaxLagWaitCancelledError
    ):
        return MaxLagTimeoutFailure(type(error))
    return ExceptionFailure(type(error))


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
        reason = result.reason
        if isinstance(reason, SkipReason):
            if result.outcome is ProjectOutcome.SKIPPED:
                self.skips[reason] += 1
        elif isinstance(reason, UnavailableReason):
            self.unavailable[reason] += 1
        elif reason is not None:
            self.failures[reason] += 1

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
