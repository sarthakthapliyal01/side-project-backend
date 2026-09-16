from pydantic import BaseModel, Field
from typing import Optional, List, Any
from datetime import datetime


class SprintBreakdownItem(BaseModel):
    sprintId: Optional[Any] = None
    sprintName: Optional[str] = None
    sprintStartDate: Optional[datetime] = None
    sprintEndDate: Optional[datetime] = None
    state: Optional[str] = None
    atStartOfSprint: float = 0.0
    addedToVersion: float = 0.0
    removedFromVersion: float = 0.0
    completed: float = 0.0
    remaining: float = 0.0


class ReleaseBurndownData(BaseModel):
    originalEstimateAtStart: float = 0.0
    completed: float = 0.0
    sprintBreakdown: List[SprintBreakdownItem] = []
    actualSprintLength: Optional[float] = None


class JiraRelease(BaseModel):
    id: Optional[Any] = Field(default=None, alias="_id")
    boardId: Optional[Any] = None
    projectId: Optional[Any] = None
    projectKey: Optional[str] = None
    releaseName: str
    companyId: Optional[Any] = None
    assignees: List[str] = []
    releaseDate: Optional[datetime] = None
    startDate: Optional[datetime] = None
    status: Optional[str] = None
    releaseBurndownData: Optional[ReleaseBurndownData] = None
    createdAt: Optional[datetime] = Field(default_factory=datetime.utcnow)
    updatedAt: Optional[datetime] = Field(default_factory=datetime.utcnow)

