from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import db, meta_db, client, MONGO_URL
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, EmailStr #better for taking input for jira like email api
from datetime import datetime #this is for jira 
from models.board import Board

class JiraConnectionRequest(BaseModel):
    jira_host: str
    jira_email: EmailStr
    jira_token: str

class GitHubConnectionRequest(BaseModel):
    github_owner: str
    github_token: str


class SaveGitHubRequest(BaseModel):
    companyName: str
    github_owner: str
    github_token: str

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "Backend is working"}

@app.get("/test-db")
async def test_db():
    collections = await db.list_collection_names()
    return {"collections": collections}

@app.post("/users")
async def create_user(user: dict):

    existing_user = await db.users.find_one(
        {"email": user["email"]}
    )

    if existing_user:
        return {"message": "User already exists"}

    result = await db.users.insert_one(user)

    return {
        "message": "User added",
        "id": str(result.inserted_id)
    }

@app.get("/users") # in this part the users in side project sections get stores all the gmail logins
async def get_users():
    users = []

    async for user in db.users.find():
        user["_id"] = str(user["_id"])
        users.append(user)

    return users

@app.get("/boards/{company_name}") #this is board secton get and post
async def get_boards(company_name: str):

    tenant_db = client[company_name]

    boards = []

    async for board in tenant_db.boards.find():
        board["_id"] = str(board["_id"])
        boards.append(board)

    return boards

@app.post("/boards/{company_name}")
async def create_board(company_name: str, board: Board):

    tenant_db = client[company_name]

    existing_board = await tenant_db.boards.find_one(
        {"boardId": board.boardId}
    )

    if existing_board:
        return {
            "message": "Board already exists"
        }

    board_data = board.model_dump()

    result = await tenant_db.boards.insert_one(board_data)

    return {
        "message": "Board created",
        "id": str(result.inserted_id)
    }

@app.post("/companies")  # This is the part where different company DBs are created
async def create_company(company: dict):

    existing_company = await meta_db.companies.find_one(
        {"companyName": company["companyName"]}
    )

    print("Searching for:", company["companyName"])
    print("Found:", existing_company)

    if existing_company:
        return {"message": "Company already exists"}

    database_name = company["companyName"].lower().replace(" ", "_")

    database_uri = (
        f"mongodb://localhost:27017/{database_name}"
        "?directConnection=true&tls=true&retryWrites=true"
    )

    company_data = {
        "companyName": company["companyName"],
        "host": company["host"],

        "databaseName": database_name,
        "databaseUri": database_uri,

        "isActive": True,
        "isRegistered": True,

        "roleRates": [],
        "holidayList": [],
        "customFields": [],

        "syncStatus": False,

        "createdAt": datetime.utcnow(),
        "updatedAt": datetime.utcnow()
    }

    # create empty company collection automatically 
    result = await meta_db.companies.insert_one(company_data)

    tenant_db = client[database_name]

    print("Creating tenant DB:", company["companyName"])

    await tenant_db.company.insert_one(company_data)

    return {
        "message": "Company created",
        "id": str(result.inserted_id)
    }

# This is Where jira integration happens
@app.post("/jira/test-connection")
async def test_jira_connection(data: JiraConnectionRequest):
    jira_host = data.jira_host.replace(".atlassian.net", "").replace("https://", "")
    jira_email = data.jira_email
    jira_token = data.jira_token

    url = f"https://{jira_host}.atlassian.net/rest/api/3/project"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(jira_email, jira_token),
            headers={"Accept": "application/json"}
        )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid Jira credentials")

    projects = response.json()
    return {
        "connected": True,
        "project_count": len(projects),
        "projects": projects
    }

class SaveJiraRequest(BaseModel):
    companyName: str
    jira_host: str
    jira_email: EmailStr
    jira_token: str

# this part stores data in mongodb tenant db
@app.post("/jira/save-connection")
async def save_jira_connection(data: SaveJiraRequest):
    database_name = data.companyName.lower().replace(" ", "_")
    tenant_db = client[database_name]
    
    connection_data = {
        "integrationType": "jira",
        "jira_host": data.jira_host,
        "jira_email": data.jira_email,
        "jira_token": data.jira_token, 
        "status": "connected",
        "updatedAt": datetime.utcnow()
    }

    await tenant_db.connections.update_one(
        {"integrationType": "jira"},
        {"$set": connection_data},
        upsert=True
    )

    return {"message": "Jira connection saved successfully"}

@app.get("/jira/connection/{company_name}")
async def get_jira_connection(company_name: str):

    tenant_db = client[company_name]

    connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not connection:
        return {"message": "No Jira connection found"}

    connection["_id"] = str(connection["_id"])

    return connection

@app.get("/jira/boards/{company_name}")
async def get_jira_boards(company_name: str):

    tenant_db = client[company_name]

    connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not connection:
        raise HTTPException(
            status_code=404,
            detail="Jira connection not found"
        )

    jira_host = connection["jira_host"].replace(
        ".atlassian.net", ""
    ).replace(
        "https://", ""
    )

    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(
                connection["jira_email"],
                connection["jira_token"]
            ),
            headers={"Accept": "application/json"}
        )

    return response.json()

@app.post("/jira/sync-boards/{company_name}") #jira sync part 
async def sync_jira_boards(company_name: str):

    tenant_db = client[company_name]

    jira_connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not jira_connection:
        return {
            "message": "Jira connection not found"
        }

    jira_host = jira_connection["jira_host"].replace(
        ".atlassian.net", ""
    ).replace(
        "https://", ""
    )

    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(
                jira_connection["jira_email"],
                jira_connection["jira_token"]
            ),
            headers={"Accept": "application/json"}
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail="Failed to fetch Jira boards"
        )

    boards = response.json()["values"]

    # Save boards to MongoDB
    for board in boards:

        board_data = {
            "boardId": board["id"],
            "boardName": board["name"],
            "boardType": board["type"],
            "boardSelf": board.get("self"),
            "isPrivate": board.get("isPrivate", False),
            "boardLocation": {
                "projectId": board.get("location", {}).get("projectId"),
                "projectName": board.get("location", {}).get("projectName"),
                "projectKey": board.get("location", {}).get("projectKey"),
                "projectTypeKey": board.get("location", {}).get("projectTypeKey"),
                "avatarURI": board.get("location", {}).get("avatarURI"),
                "displayName": board.get("location", {}).get("displayName"),
                "name": board.get("location", {}).get("name"),
                "githubProjectV2NodeId": None,
                "githubResourceKind": None
            }
        }

        await tenant_db.boards.update_one(
            {"boardId": board["id"]},
            {"$set": board_data},
            upsert=True
        )

    return {
        "message": "Boards synced successfully",
        "totalBoards": len(boards)
    }

@app.post("/jira/sync-projects/{company_name}")
async def sync_jira_projects(company_name: str):

    tenant_db = client[company_name]

    # Get Jira connection
    jira_connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not jira_connection:
        raise HTTPException(
            status_code=404,
            detail="Jira connection not found"
        )

    # Build Jira URL
    jira_host = jira_connection["jira_host"] \
        .replace(".atlassian.net", "") \
        .replace("https://", "")

    url = f"https://{jira_host}.atlassian.net/rest/api/3/project"

    # Fetch projects from Jira
    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(
                jira_connection["jira_email"],
                jira_connection["jira_token"]
            ),
            headers={"Accept": "application/json"}
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail="Failed to fetch Jira projects"
        )

    jira_projects = response.json()

    # Save projects into MongoDB
    for project in jira_projects:

        project_data = {
            "projectId": str(project.get("id")),
            "projectName": project.get("name"),
            "projectKey": project.get("key"),
            "projectType": project.get("projectTypeKey"),

            # UI fields
            "isSelected": False,
            "hideStatus": False,

            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        await tenant_db.projects.update_one(
            {
                "projectId": str(project.get("id"))
            },
            {
                "$set": {
                    "projectName": project.get("name"),
                    "projectKey": project.get("key"),
                    "projectType": project.get("projectTypeKey"),
                    "updatedAt": datetime.utcnow()
                },
                "$setOnInsert": {
                    "projectId": str(project.get("id")),
                    "isSelected": False,
                    "hideStatus": False,
                    "createdAt": datetime.utcnow()
                }
            },
            upsert=True
        )

    return {
        "message": "Projects synced successfully",
        "totalProjects": len(jira_projects)
    }

@app.get("/jira/projects/{company_name}")
async def get_jira_projects(company_name: str):

    tenant_db = client[company_name]

    projects = await tenant_db.projects.find().to_list(None)

    for project in projects:
        project["_id"] = str(project["_id"])

    return {
        "totalProjects": len(projects),
        "projects": projects
    }

# This is where GitHub connection is tested
@app.post("/github/test-connection")
async def test_github_connection(data: GitHubConnectionRequest):

    headers = {
        "Authorization": f"Bearer {data.github_token}",
        "Accept": "application/vnd.github+json"
    }

    async with httpx.AsyncClient() as httpx_client:

        # Check whether the owner (user or organization) exists
        response = await httpx_client.get(
            f"https://api.github.com/users/{data.github_owner}",
            headers=headers
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail="Invalid GitHub owner or Personal Access Token"
        )

    owner = response.json()

    return {
        "connected": True,
        "owner": owner["login"],
        "type": owner["type"]
    }

# This stores GitHub connection inside the tenant database
@app.post("/github/save-connection")
async def save_github_connection(data: SaveGitHubRequest):

    database_name = data.companyName.lower().replace(" ", "_")
    tenant_db = client[database_name]

    connection_data = {
        "integrationType": "github",
        "github_owner": data.github_owner,
        "github_token": data.github_token,
        "status": "connected",
        "updatedAt": datetime.utcnow()
    }

    await tenant_db.connections.update_one(
        {"integrationType": "github"},
        {"$set": connection_data},
        upsert=True
    )

    return {
        "message": "GitHub connection saved successfully"
    }