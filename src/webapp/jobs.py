"""In-memory job runner with progress streaming."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.queued
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    resume_from: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def append(self, line: str) -> None:
        with self._cond:
            self.lines.append(line)
            self._cond.notify_all()

    def finish(self, *, status: JobStatus, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self._cond:
            self.status = status
            if result is not None:
                self.result = result
            self.error = error
            self.finished_at = time.time()
            self._cond.notify_all()

    def wait_lines(self, start: int, timeout: float = 1.0) -> tuple[list[str], JobStatus]:
        with self._cond:
            if start >= len(self.lines) and self.status in (JobStatus.queued, JobStatus.running):
                self._cond.wait(timeout=timeout)
            return self.lines[start:], self.status


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(
        self,
        kind: str,
        runner: Callable[[Job], None],
        *,
        request: dict[str, Any] | None = None,
        resume_from: str | None = None,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            request=dict(request or {}),
            resume_from=resume_from,
        )
        with self._lock:
            self._jobs[job.id] = job

        def _run() -> None:
            job.status = JobStatus.running
            try:
                runner(job)
            except Exception as exc:  # noqa: BLE001
                job.finish(status=JobStatus.failed, error=str(exc))

        threading.Thread(target=_run, name=f"job-{job.id}", daemon=True).start()
        return job

    def iter_sse(self, job: Job) -> Iterator[dict[str, Any]]:
        index = 0
        yield {
            "type": "status",
            "job_id": job.id,
            "status": job.status.value,
            "kind": job.kind,
            "resume_from": job.resume_from,
        }
        while True:
            lines, status = job.wait_lines(index, timeout=1.0)
            for line in lines:
                yield {"type": "log", "line": line}
            index += len(lines)
            if status in (JobStatus.succeeded, JobStatus.failed):
                payload: dict[str, Any] = {
                    "type": "done",
                    "status": status.value,
                    "result": job.result,
                    "kind": job.kind,
                    "job_id": job.id,
                    "can_retry": status == JobStatus.failed and bool(job.request),
                }
                if job.error:
                    payload["error"] = job.error
                if job.resume_from:
                    payload["resume_from"] = job.resume_from
                yield payload
                return
