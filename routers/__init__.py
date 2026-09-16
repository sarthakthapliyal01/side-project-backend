from .general import router as general_router
from .jira import router as jira_router
from .github import router as github_router
from .gitlab import router as gitlab_router
from .azure_boards import router as azure_boards_router

__all__ = ["general_router", "jira_router", "github_router", "gitlab_router", "azure_boards_router"]

