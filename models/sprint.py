from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class CommittedVsCompletedMetrics(BaseModel):
    committedStoryPoints: Optional[float] = 0.0
    completedStoryPoints: Optional[float] = 0.0
    committedOriginalEstimateHrs: Optional[float] = 0.0
    completedOriginalEstimateHrs: Optional[float] = 0.0


class DevBurnupEstimate(BaseModel):
    assignee: str
    initialStoryPoints: Optional[float] = 0.0
    initialOriginalEstimateHrs: Optional[float] = 0.0


class ReleaseInfo(BaseModel):
    name: Optional[str] = None
    releaseDate: Optional[datetime] = None


class Sprint(BaseModel):
    sprintId: int
    name: str
    state: Optional[str] = None  # active, closed, future
    boardId: Optional[int] = None
    boardObjectId: Optional[str] = None
    projectId: Optional[str] = None
    projectKey: Optional[str] = None
    companyId: Optional[str] = None
    companyName: Optional[str] = None
    projectKeyId: Optional[int] = None
    iterations: List[str] = []
    githubProjectV2IterationId: Optional[str] = None
    originBoardId: Optional[int] = None
    boardReference: Optional[int] = None
    totalStoryPoints: Optional[float] = 0.0
    committedVsCompletedMetrics: Optional[CommittedVsCompletedMetrics] = None
    idealBurnupByDev: List[DevBurnupEstimate] = []
    startDate: Optional[datetime] = None
    endDate: Optional[datetime] = None
    completeDate: Optional[datetime] = None
    totalDays: Optional[float] = None
    releases: List[ReleaseInfo] = []
    createdAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
    updatedAt: Optional[datetime] = Field(default_factory=datetime.utcnow)


class IssueType(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


class StatusType(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None


class SprintIssue(BaseModel):
    issueId: int
    key: str
    summary: str
    sprintId: List[Any] = []
    boardId: Optional[int] = None
    projectId: Optional[str] = None
    projectKey: Optional[str] = None
    companyId: Optional[str] = None
    companyName: Optional[str] = None
    originalEstimateHrs: Optional[float] = 0.0
    developer: List[str] = []
    timeSpentHrs: Optional[float] = 0.0
    storyPoints: Optional[float] = 0.0
    pointsSourceType: Optional[str] = None
    pointsSourceRefName: Optional[str] = None
    iteration: Optional[str] = None
    iterationSourceType: Optional[str] = None
    iterationSourceRefName: Optional[str] = None
    type: Optional[IssueType] = None
    sprint: Optional[Any] = None
    status: Optional[StatusType] = None
    issueCreatedAt: Optional[datetime] = None
    issueUpdatedAt: Optional[datetime] = None
    workStartedAt: Optional[datetime] = None
    workCompletedAt: Optional[datetime] = None
    assignee: Optional[str] = None
    projectKeyId: Optional[int] = None
    priority: Optional[str] = None
    fixVersion: Optional[str] = None
    fixVersionNames: List[str] = []
    affectedVersion: Optional[List[str]] = None
    label: List[str] = []
    blockedBy: Optional[List[str]] = None
    relatesTo: Optional[List[str]] = None
    duedate: Optional[datetime] = None
    cycleTimeSpent: Optional[str] = None
    backflowRate: Optional[float] = 0.0
    createdAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
    updatedAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
