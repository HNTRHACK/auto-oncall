import hashlib
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StackFrame(BaseModel):
    filename: str = Field(min_length=1, max_length=1000)
    lineno: int = Field(ge=0)
    function: str = Field(max_length=500)


class CrashPayload(BaseModel):
    service_name: str = Field(min_length=1, max_length=200)
    environment: str = Field(min_length=1, max_length=100)
    error_type: str = Field(min_length=1, max_length=200)
    error_message: str = Field(max_length=10_000)
    # Index 0 is the top frame (where the error was raised).
    stack_trace: list[StackFrame] = Field(min_length=1, max_length=200)
    timestamp: datetime
    # Optional link back to the source of the crash report (e.g. the Sentry issue page).
    source_url: str | None = Field(default=None, max_length=2000, pattern=r"^https://")


_NORMALIZERS = [
    re.compile(r"(?<!\w)'[^']*'(?!\w)|\"[^\"]*\""),  # quoted values (not apostrophes)
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"\b0x[0-9a-f]+\b", re.I),
    re.compile(r"\d+(?:\.\d+)?"),
]


def normalize_message(message: str) -> str:
    for pattern in _NORMALIZERS:
        message = pattern.sub("", message)
    return " ".join(message.split())


def fingerprint(payload: CrashPayload) -> str:
    top = payload.stack_trace[0]
    parts = [
        payload.service_name,
        normalize_message(payload.error_message),
        f"{top.filename}:{top.function}:{top.lineno}",
    ]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


class FileDiff(BaseModel):
    path: str
    status: str = "modified"
    patch: str = ""


class Commit(BaseModel):
    sha: str
    author: str
    message: str
    files: list[FileDiff] = []


class SourceFile(BaseModel):
    content: str
    blob_sha: str


class Runbook(BaseModel):
    id: str
    title: str
    content: str


class RCAReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspected_commit_sha: str
    culprit_author: str
    root_cause: str
    confidence_score: float = Field(ge=0, le=1)
    affected_file: str
    file_patch: str  # FULL new content of affected_file
