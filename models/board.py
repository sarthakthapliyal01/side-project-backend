from pydantic import BaseModel
from typing import Optional


class BoardLocation(BaseModel):
    projectId: Optional[int] = None
    projectName: Optional[str] = None
    projectKey: Optional[str] = None
    projectTypeKey: Optional[str] = None
    avatarURI: Optional[str] = None
    displayName: Optional[str] = None
    name: Optional[str] = None

    githubProjectV2NodeId: Optional[str] = None
    githubResourceKind: Optional[str] = None


class Board(BaseModel):
    boardId: int
    boardName: str
    boardType: str

    boardSelf: Optional[str] = None
    isPrivate: bool = False

    boardLocation: Optional[BoardLocation] = None