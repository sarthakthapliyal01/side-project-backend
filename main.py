from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import general_router, jira_router, github_router

app = FastAPI(title="QMetrix Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(general_router)
app.include_router(jira_router)
app.include_router(github_router)
