from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import general_router, jira_router, github_router, gitlab_router, azure_boards_router

import os

app = FastAPI(title="QMetrix Backend API")

allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()] if allowed_origins_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(general_router)
app.include_router(jira_router)
app.include_router(github_router)
app.include_router(gitlab_router)
app.include_router(azure_boards_router)

