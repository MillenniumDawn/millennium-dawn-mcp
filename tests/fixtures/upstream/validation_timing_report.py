"""Small public-API fixture for the upstream CI timing report helper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


@dataclass(frozen=True)
class TimingStep:
    name: str
    status: str | None
    conclusion: str | None
    duration_seconds: float | None


@dataclass(frozen=True)
class TimingJob:
    name: str
    status: str | None
    conclusion: str | None
    duration_seconds: float | None
    steps: list[TimingStep]


@dataclass(frozen=True)
class TimingSample:
    database_id: str
    attempt: int
    head_sha: str
    status: str | None
    conclusion: str | None
    overall_seconds: float | None
    jobs: list[TimingJob]
    metadata: dict[str, Any]
    metadata_error: str | None
    cache_label: str
    _compatibility_key: str | None

    @property
    def summed_job_seconds(self) -> float | None:
        values = [job.duration_seconds for job in self.jobs if job.duration_seconds is not None]
        return sum(values) if values else None

    @property
    def eligible_for_summary(self) -> bool:
        return (
            self.conclusion == "success"
            and self.metadata_error is None
            and self.overall_seconds is not None
        )

    @property
    def compatibility_key(self) -> str | None:
        return self._compatibility_key


def parse_export(export: dict[str, Any], _path: Path) -> TimingSample:
    metadata = export.get("metadata") or {}
    metadata_error = None if isinstance(metadata, dict) and metadata else "metadata unavailable"
    jobs = [
        TimingJob(
            name=str(job.get("name", "")),
            status=job.get("status"),
            conclusion=job.get("conclusion"),
            duration_seconds=_seconds(job.get("startedAt"), job.get("completedAt")),
            steps=[
                TimingStep(
                    name=str(step.get("name", "")),
                    status=step.get("status"),
                    conclusion=step.get("conclusion"),
                    duration_seconds=_seconds(step.get("startedAt"), step.get("completedAt")),
                )
                for step in job.get("steps", [])
            ],
        )
        for job in export.get("jobs", [])
    ]
    cache_label = str(metadata.get("cache", "unknown"))
    key = {
        "headSha": export.get("headSha", ""),
        "metadata": metadata,
    }
    return TimingSample(
        database_id=str(export.get("databaseId", "")),
        attempt=int(export.get("attempt", 1)),
        head_sha=str(export.get("headSha", "")),
        status=export.get("status"),
        conclusion=export.get("conclusion"),
        overall_seconds=_seconds(export.get("startedAt"), export.get("updatedAt")),
        jobs=jobs,
        metadata=metadata,
        metadata_error=metadata_error,
        cache_label=cache_label,
        _compatibility_key=json.dumps(key, sort_keys=True, separators=(",", ":"))
        if metadata_error is None
        else None,
    )
