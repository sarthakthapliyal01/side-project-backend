from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class ReviewSchema(BaseModel):
    id: Optional[Any] = None
    user: Optional[str] = None
    state: Optional[str] = None
    submittedAt: Optional[datetime] = None
    body: Optional[str] = None


class CommitSchema(BaseModel):
    sha: Optional[str] = None
    author: Optional[str] = None
    message: Optional[str] = None
    date: Optional[datetime] = None


class SensitiveFileSchema(BaseModel):
    filename: str
    status: Optional[str] = None
    additions: Optional[int] = 0
    deletions: Optional[int] = 0


class MissingTestsSchema(BaseModel):
    hasMissingTests: bool = False
    codeFilesChanged: int = 0
    testFilesChanged: int = 0


class PullRequest(BaseModel):
    companyId: Optional[Any] = None
    projectId: Optional[Any] = None
    boardId: Optional[Any] = None
    fixVersion: Optional[str] = None
    repo: str
    sprintId: List[Any] = []
    title: str
    projectKey: str
    status: str
    prId: str
    prCreatedAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
    prClosedAt: Optional[datetime] = None
    prMergedAt: Optional[datetime] = None
    prCreatedBy: str
    prMergedBy: Optional[str] = None
    filesChanged: int = 0
    linesAdded: int = 0
    linesDeleted: int = 0
    reviewComments: int = 0
    mergeable: Optional[str] = None
    merged: Optional[str] = None
    prNumber: Optional[int] = None
    branchName: Optional[str] = None
    reviews: List[ReviewSchema] = []
    commits: List[CommitSchema] = []
    hasSensitiveChanges: bool = False
    sensitiveFiles: List[SensitiveFileSchema] = []
    missingTests: MissingTestsSchema = Field(default_factory=MissingTestsSchema)

    createdAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
    updatedAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
