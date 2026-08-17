from pydantic import BaseModel
from typing import Optional, Any


class BoardLocation(BaseModel):
    projectId: Optional[Any] = None
    projectName: Optional[str] = None
    projectKey: Optional[str] = None
    projectTypeKey: Optional[str] = None
    avatarURI: Optional[str] = None
    displayName: Optional[str] = None
    name: Optional[str] = None

    githubProjectV2NodeId: Optional[str] = None
    githubResourceKind: Optional[str] = None


class Board(BaseModel):
    boardId: Any
    boardName: str
    boardType: str
    companyId: Optional[Any] = None
    projectId: Optional[Any] = None

    boardSelf: Optional[str] = None
    isPrivate: bool = False

    boardLocation: Optional[BoardLocation] = None