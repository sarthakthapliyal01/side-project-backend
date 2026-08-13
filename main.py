from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import db, meta_db, client, MONGO_URL
import httpx
import re
from fastapi import HTTPException
from pydantic import BaseModel, EmailStr #better for taking input for jira like email api
from datetime import datetime #this is for jira 
from models.board import Board
from models.sprint import Sprint, SprintIssue 

class JiraConnectionRequest(BaseModel):
    jira_host: str
    jira_email: EmailStr
    jira_token: str

class ProjectSelectionRequest(BaseModel):
    projectId: str
    isSelected: bool

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

@app.post("/companies")  # Creates meta_db record and initializes tenant database in MongoDB
async def create_company(company: dict):
    company_name = company.get("companyName", "").strip()

    if not company_name:
        raise HTTPException(status_code=400, detail="Company name is required")

    database_name = company_name.lower().replace(" ", "_")
    database_uri = (
        f"mongodb://localhost:27017/{database_name}"
        "?directConnection=true&tls=true&retryWrites=true"
    )

    company_data = {
        "companyName": company_name,
        "databaseName": database_name,
        "databaseUri": database_uri,
        "isActive": True,
        "isRegistered": True,
        "roleRates": [],
        "holidayList": [],
        "customFields": [],
        "syncStatus": False,
        "updatedAt": datetime.utcnow()
    }

    meta_result = await meta_db.companies.update_one(
        {"companyName": company_name},
        {"$set": company_data, "$setOnInsert": {"createdAt": datetime.utcnow()}},
        upsert=True
    )

    tenant_db = client[database_name]
    await tenant_db.company.update_one(
        {"companyName": company_name},
        {"$set": company_data, "$setOnInsert": {"createdAt": datetime.utcnow()}},
        upsert=True
    )

    print(f"Tenant DB '{database_name}' successfully initialized in MongoDB.")

    return {
        "message": "Company workspace ready",
        "companyName": company_name,
        "databaseName": database_name
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

def get_tenant_db(company_name: str):
    db_name = company_name.lower().replace(" ", "_").strip()
    return client[db_name]

@app.get("/jira/connection/{company_name}")
async def get_jira_connection(company_name: str):
    tenant_db = get_tenant_db(company_name)

    connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not connection:
        return {
            "connected": False
        }

    return {
        "connected": True,
        "jira_host": connection["jira_host"],
        "jira_email": connection["jira_email"]
    }

@app.get("/jira/boards/{company_name}")
async def get_jira_boards(company_name: str):
    tenant_db = get_tenant_db(company_name)

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
    tenant_db = get_tenant_db(company_name)

    company_doc = await meta_db.companies.find_one({"companyName": company_name})
    if not company_doc:
        db_name = company_name.lower().replace(" ", "_").strip()
        company_doc = await meta_db.companies.find_one({"databaseName": db_name})
    if not company_doc:
        company_doc = await meta_db.companies.find_one({
            "companyName": {"$regex": f"^{re.escape(company_name)}$", "$options": "i"}
        })

    company_id = str(company_doc["_id"]) if company_doc and "_id" in company_doc else None

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
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}

    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=auth,
            headers=headers
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail="Failed to fetch Jira boards"
        )

    boards = response.json().get("values", [])

    # 1. Save boards to MongoDB
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
        if company_id:
            board_data["companyId"] = company_id

        await tenant_db.boards.update_one(
            {"boardId": board["id"]},
            {"$set": board_data},
            upsert=True
        )

    # 2. Save Sprints & Sprint Issues to MongoDB
    synced_sprints_count = 0
    synced_issues_count = 0

    async with httpx.AsyncClient() as httpx_client:
        for board in boards:
            board_id = board["id"]
            board_doc = await tenant_db.boards.find_one({"boardId": board_id})
            board_mongo_id = str(board_doc["_id"]) if board_doc and "_id" in board_doc else None

            sprint_url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board/{board_id}/sprint"
            sprint_res = await httpx_client.get(sprint_url, auth=auth, headers=headers)
            if sprint_res.status_code != 200:
                continue

            sprint_values = sprint_res.json().get("values", [])
            for s in sprint_values:
                sprint_id = s.get("id")
                if not sprint_id:
                    continue

                start_dt = datetime.fromisoformat(s["startDate"].replace("Z", "+00:00")) if s.get("startDate") else None
                end_dt = datetime.fromisoformat(s["endDate"].replace("Z", "+00:00")) if s.get("endDate") else None
                complete_dt = datetime.fromisoformat(s["completeDate"].replace("Z", "+00:00")) if s.get("completeDate") else None

                total_days = None
                if start_dt and end_dt:
                    total_days = (end_dt - start_dt).total_seconds() / 86400.0

                project_id = board.get("location", {}).get("projectId")
                project_key = board.get("location", {}).get("projectKey")

                sprint_doc = {
                    "sprintId": sprint_id,
                    "name": s.get("name"),
                    "state": s.get("state"),
                    "boardId": board_id,
                    "boardObjectId": board_mongo_id,
                    "originBoardId": s.get("originBoardId"),
                    "projectId": str(project_id) if project_id else None,
                    "projectKey": project_key,
                    "companyName": company_name,
                    "startDate": start_dt,
                    "endDate": end_dt,
                    "completeDate": complete_dt,
                    "totalDays": total_days,
                    "updatedAt": datetime.utcnow()
                }
                if company_id:
                    sprint_doc["companyId"] = company_id

                await tenant_db.sprints.update_one(
                    {"sprintId": sprint_id},
                    {
                        "$set": sprint_doc,
                        "$setOnInsert": {"createdAt": datetime.utcnow()}
                    },
                    upsert=True
                )
                synced_sprints_count += 1

                # 3. Sync issues for this sprint
                issues_url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{sprint_id}/issue"
                issues_res = await httpx_client.get(issues_url, auth=auth, headers=headers)
                if issues_res.status_code == 200:
                    issues_data = issues_res.json().get("issues", [])
                    for issue in issues_data:
                        fields = issue.get("fields", {})
                        issue_id = int(issue.get("id"))
                        key = issue.get("key")
                        summary = fields.get("summary", "")

                        issue_project = fields.get("project", {})
                        issue_project_id = issue_project.get("id") or project_id
                        issue_project_key = issue_project.get("key") or project_key

                        story_points = fields.get("customfield_10016") or fields.get("customfield_10026") or 0.0

                        timetracking = fields.get("timetracking", {})
                        original_estimate_hrs = (timetracking.get("originalEstimateSeconds", 0) or 0) / 3600.0
                        time_spent_hrs = (timetracking.get("timespentSeconds", 0) or 0) / 3600.0

                        assignee_data = fields.get("assignee")
                        assignee_name = assignee_data.get("displayName") if assignee_data else None

                        issue_type_data = fields.get("issuetype", {})
                        status_data = fields.get("status", {})
                        priority_data = fields.get("priority", {})

                        created_dt = datetime.fromisoformat(fields["created"].replace("Z", "+00:00")) if fields.get("created") else None
                        updated_dt = datetime.fromisoformat(fields["updated"].replace("Z", "+00:00")) if fields.get("updated") else None
                        duedate_dt = datetime.fromisoformat(fields["duedate"]) if fields.get("duedate") else None

                        labels = fields.get("labels", [])
                        fix_versions = [v.get("name") for v in fields.get("fixVersions", [])]

                        issue_doc = {
                            "issueId": issue_id,
                            "key": key,
                            "summary": summary,
                            "sprintId": [sprint_id],
                            "boardId": board_id,
                            "boardObjectId": board_mongo_id,
                            "projectId": str(issue_project_id) if issue_project_id else None,
                            "projectKey": issue_project_key,
                            "companyName": company_name,
                            "storyPoints": float(story_points) if story_points else 0.0,
                            "originalEstimateHrs": original_estimate_hrs,
                            "timeSpentHrs": time_spent_hrs,
                            "assignee": assignee_name,
                            "developer": [assignee_name] if assignee_name else [],
                            "type": {
                                "id": issue_type_data.get("id"),
                                "name": issue_type_data.get("name"),
                                "description": issue_type_data.get("description")
                            },
                            "status": {
                                "id": status_data.get("id"),
                                "name": status_data.get("name")
                            },
                            "priority": priority_data.get("name") if priority_data else None,
                            "issueCreatedAt": created_dt,
                            "issueUpdatedAt": updated_dt,
                            "duedate": duedate_dt,
                            "label": labels,
                            "fixVersionNames": fix_versions,
                            "updatedAt": datetime.utcnow()
                        }
                        if company_id:
                            issue_doc["companyId"] = company_id

                        await tenant_db.sprint_issues.update_one(
                            {"issueId": issue_id},
                            {
                                "$set": issue_doc,
                                "$setOnInsert": {"createdAt": datetime.utcnow()}
                            },
                            upsert=True
                        )
                        synced_issues_count += 1

    # Backfill companyId & boardObjectId in boards, sprints, sprint_issues
    async for b in tenant_db.boards.find():
        if b.get("boardId") and "_id" in b:
            b_id = b["boardId"]
            b_mongo_id = str(b["_id"])
            await tenant_db.sprints.update_many(
                {"boardId": b_id},
                {"$set": {"boardObjectId": b_mongo_id}}
            )
            await tenant_db.sprint_issues.update_many(
                {"boardId": b_id},
                {"$set": {"boardObjectId": b_mongo_id}}
            )

    if company_id:
        await tenant_db.boards.update_many(
            {"$or": [{"companyId": {"$exists": False}}, {"companyId": None}, {"companyId": ""}]},
            {"$set": {"companyId": company_id}}
        )
        await tenant_db.sprints.update_many(
            {"$or": [{"companyId": {"$exists": False}}, {"companyId": None}, {"companyId": ""}]},
            {"$set": {"companyId": company_id}}
        )
        await tenant_db.sprint_issues.update_many(
            {"$or": [{"companyId": {"$exists": False}}, {"companyId": None}, {"companyId": ""}]},
            {"$set": {"companyId": company_id}}
        )

    return {
        "message": "Boards, Sprints, and Sprint Issues synced successfully",
        "totalBoards": len(boards),
        "totalSprints": synced_sprints_count,
        "totalSprintIssues": synced_issues_count
    }


@app.post("/jira/sync-projects/{company_name}")
async def sync_jira_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)

    # Fetch company document from QMetrixMetaDB.companies to get companyId
    company_doc = await meta_db.companies.find_one({"companyName": company_name})
    if not company_doc:
        db_name = company_name.lower().replace(" ", "_").strip()
        company_doc = await meta_db.companies.find_one({"databaseName": db_name})
    if not company_doc:
        company_doc = await meta_db.companies.find_one({
            "companyName": {"$regex": f"^{re.escape(company_name)}$", "$options": "i"}
        })

    company_id = str(company_doc["_id"]) if company_doc and "_id" in company_doc else None

    jira_connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not jira_connection:
        raise HTTPException(
            status_code=404,
            detail="Jira connection not found"
        )

    jira_host = jira_connection["jira_host"] \
        .replace(".atlassian.net", "") \
        .replace("https://", "")

    url = f"https://{jira_host}.atlassian.net/rest/api/3/project"

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

    for project in jira_projects:
        project_id = str(project.get("id"))

        set_fields = {
            "projectName": project.get("name"),
            "projectKey": project.get("key"),
            "projectType": project.get("projectTypeKey"),
            "updatedAt": datetime.utcnow()
        }
        set_on_insert_fields = {
            "projectId": project_id,
            "isSelected": False,
            "hideStatus": False,
            "createdAt": datetime.utcnow()
        }

        if company_id:
            set_fields["companyId"] = company_id

        await tenant_db.projects.update_one(
            {
                "projectId": project_id
            },
            {
                "$set": set_fields,
                "$setOnInsert": set_on_insert_fields
            },
            upsert=True
        )

    return {
        "message": "Projects synced successfully",
        "totalProjects": len(jira_projects)
    }

@app.get("/jira/projects/{company_name}")
async def get_jira_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)

    projects = await tenant_db.projects.find().to_list(None)

    for project in projects:
        project["_id"] = str(project["_id"])

    return {
        "totalProjects": len(projects),
        "projects": projects
    }

# this will select project in the dashboard
@app.put("/jira/project-selection/{company_name}")
async def update_project_selection(
    company_name: str,
    data: ProjectSelectionRequest
):
    tenant_db = get_tenant_db(company_name)

    result = await tenant_db.projects.update_one(
        {"projectId": data.projectId},
        {
            "$set": {
                "isSelected": data.isSelected,
                "updatedAt": datetime.utcnow()
            }
        }
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=404,
            detail="Project not found"
        )

    return {
        "message": "Project selection updated"
    }

@app.get("/jira/selected-projects/{company_name}")
async def get_selected_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)

    projects = await tenant_db.projects.find(
        {"isSelected": True}
    ).to_list(length=None)

    for project in projects:
        project["_id"] = str(project["_id"])

    return {
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

@app.get("/jira/sprints/{company_name}")
async def get_jira_sprints(company_name: str, project_id: str = None):
    database_name = company_name.lower().replace(" ", "_")
    tenant_db = client[database_name]

    jira_connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not jira_connection:
        return {"sprints": []}

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    jira_email = jira_connection["jira_email"]
    jira_token = jira_connection["jira_token"]

    auth = (jira_email, jira_token)
    headers = {"Accept": "application/json"}

    all_sprints = []
    seen_sprint_ids = set()

    try:
        async with httpx.AsyncClient() as httpx_client:
            # Build boards URL with project_id filter if provided
            boards_url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"
            if project_id:
                boards_url += f"?projectKeyOrId={project_id}"

            boards_res = await httpx_client.get(
                boards_url,
                auth=auth,
                headers=headers,
                timeout=10.0
            )

            if boards_res.status_code == 200:
                boards_data = boards_res.json()
                boards = boards_data.get("values", [])

                for board in boards:
                    board_id = board.get("id")
                    if not board_id:
                        continue
                    sprints_res = await httpx_client.get(
                        f"https://{jira_host}.atlassian.net/rest/agile/1.0/board/{board_id}/sprint",
                        auth=auth,
                        headers=headers,
                        timeout=10.0
                    )
                    if sprints_res.status_code == 200:
                        sprint_values = sprints_res.json().get("values", [])
                        for s in sprint_values:
                            sprint_id = str(s.get("id"))
                            if sprint_id not in seen_sprint_ids:
                                seen_sprint_ids.add(sprint_id)
                                all_sprints.append({
                                    "id": sprint_id,
                                    "name": s.get("name"),
                                    "state": s.get("state"),
                                    "startDate": s.get("startDate", ""),
                                    "endDate": s.get("endDate", "")
                                })
    except Exception as e:
        print(f"Error fetching sprints from Jira API: {e}")

    return {"sprints": all_sprints}


from typing import Optional

@app.post("/jira/sync-sprints/{company_name}")
async def sync_jira_sprints(company_name: str, board_id: Optional[int] = None):
    tenant_db = get_tenant_db(company_name)

    company_doc = await meta_db.companies.find_one({"companyName": company_name})
    if not company_doc:
        db_name = company_name.lower().replace(" ", "_").strip()
        company_doc = await meta_db.companies.find_one({"databaseName": db_name})
    if not company_doc:
        company_doc = await meta_db.companies.find_one({
            "companyName": {"$regex": f"^{re.escape(company_name)}$", "$options": "i"}
        })

    company_id = str(company_doc["_id"]) if company_doc and "_id" in company_doc else None

    jira_connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not jira_connection:
        raise HTTPException(
            status_code=404,
            detail="Jira connection not found"
        )

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}

    boards_to_sync = []
    if board_id:
        boards_to_sync.append(board_id)
    else:
        async for b in tenant_db.boards.find():
            if b.get("boardId"):
                boards_to_sync.append(b["boardId"])

    if not boards_to_sync:
        async with httpx.AsyncClient() as httpx_client:
            b_res = await httpx_client.get(
                f"https://{jira_host}.atlassian.net/rest/agile/1.0/board",
                auth=auth,
                headers=headers
            )
            if b_res.status_code == 200:
                for b in b_res.json().get("values", []):
                    boards_to_sync.append(b["id"])

    synced_sprints_count = 0
    async with httpx.AsyncClient() as httpx_client:
        for b_id in boards_to_sync:
            url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board/{b_id}/sprint"
            response = await httpx_client.get(url, auth=auth, headers=headers)
            if response.status_code != 200:
                continue

            board_doc = await tenant_db.boards.find_one({"boardId": b_id})
            board_mongo_id = str(board_doc["_id"]) if board_doc and "_id" in board_doc else None
            project_id = None
            project_key = None
            if board_doc and "boardLocation" in board_doc:
                project_id = board_doc["boardLocation"].get("projectId")
                project_key = board_doc["boardLocation"].get("projectKey")

            sprint_values = response.json().get("values", [])
            for s in sprint_values:
                sprint_id = s.get("id")
                start_dt = datetime.fromisoformat(s["startDate"].replace("Z", "+00:00")) if s.get("startDate") else None
                end_dt = datetime.fromisoformat(s["endDate"].replace("Z", "+00:00")) if s.get("endDate") else None
                complete_dt = datetime.fromisoformat(s["completeDate"].replace("Z", "+00:00")) if s.get("completeDate") else None

                total_days = None
                if start_dt and end_dt:
                    total_days = (end_dt - start_dt).total_seconds() / 86400.0

                sprint_doc = {
                    "sprintId": sprint_id,
                    "name": s.get("name"),
                    "state": s.get("state"),
                    "boardId": b_id,
                    "boardObjectId": board_mongo_id,
                    "originBoardId": s.get("originBoardId"),
                    "projectId": str(project_id) if project_id else None,
                    "projectKey": project_key,
                    "companyName": company_name,
                    "startDate": start_dt,
                    "endDate": end_dt,
                    "completeDate": complete_dt,
                    "totalDays": total_days,
                    "updatedAt": datetime.utcnow()
                }
                if company_id:
                    sprint_doc["companyId"] = company_id

                await tenant_db.sprints.update_one(
                    {"sprintId": sprint_id},
                    {
                        "$set": sprint_doc,
                        "$setOnInsert": {"createdAt": datetime.utcnow()}
                    },
                    upsert=True
                )
                synced_sprints_count += 1

    if company_id:
        await tenant_db.sprints.update_many(
            {"$or": [{"companyId": {"$exists": False}}, {"companyId": None}, {"companyId": ""}]},
            {"$set": {"companyId": company_id}}
        )

    return {
        "message": "Sprints synced successfully",
        "totalSynced": synced_sprints_count
    }


@app.get("/jira/db-sprints/{company_name}")
async def get_db_sprints(company_name: str, board_id: Optional[int] = None):
    tenant_db = get_tenant_db(company_name)
    query = {}
    if board_id:
        query["boardId"] = board_id

    sprints = await tenant_db.sprints.find(query).to_list(None)
    for s in sprints:
        s["_id"] = str(s["_id"])

    return {"sprints": sprints}


@app.post("/jira/sync-sprint-issues/{company_name}/{sprint_id}")
async def sync_jira_sprint_issues(company_name: str, sprint_id: int):
    tenant_db = get_tenant_db(company_name)

    company_doc = await meta_db.companies.find_one({"companyName": company_name})
    if not company_doc:
        db_name = company_name.lower().replace(" ", "_").strip()
        company_doc = await meta_db.companies.find_one({"databaseName": db_name})
    if not company_doc:
        company_doc = await meta_db.companies.find_one({
            "companyName": {"$regex": f"^{re.escape(company_name)}$", "$options": "i"}
        })

    company_id = str(company_doc["_id"]) if company_doc and "_id" in company_doc else None

    sprint_doc = await tenant_db.sprints.find_one({"sprintId": sprint_id})
    board_id = sprint_doc.get("boardId") if sprint_doc else None
    board_mongo_id = sprint_doc.get("boardObjectId") if sprint_doc else None
    project_id = sprint_doc.get("projectId") if sprint_doc else None
    project_key = sprint_doc.get("projectKey") if sprint_doc else None

    jira_connection = await tenant_db.connections.find_one(
        {"integrationType": "jira"}
    )

    if not jira_connection:
        raise HTTPException(
            status_code=404,
            detail="Jira connection not found"
        )

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}

    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{sprint_id}/issue"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(url, auth=auth, headers=headers)

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch issues for sprint {sprint_id}"
        )

    issues_data = response.json().get("issues", [])
    synced_issues_count = 0

    for issue in issues_data:
        fields = issue.get("fields", {})
        issue_id = int(issue.get("id"))
        key = issue.get("key")
        summary = fields.get("summary", "")

        issue_project = fields.get("project", {})
        issue_project_id = issue_project.get("id") or project_id
        issue_project_key = issue_project.get("key") or project_key

        story_points = fields.get("customfield_10016") or fields.get("customfield_10026") or 0.0

        timetracking = fields.get("timetracking", {})
        original_estimate_hrs = (timetracking.get("originalEstimateSeconds", 0) or 0) / 3600.0
        time_spent_hrs = (timetracking.get("timespentSeconds", 0) or 0) / 3600.0

        assignee_data = fields.get("assignee")
        assignee_name = assignee_data.get("displayName") if assignee_data else None

        issue_type_data = fields.get("issuetype", {})
        status_data = fields.get("status", {})
        priority_data = fields.get("priority", {})

        created_dt = datetime.fromisoformat(fields["created"].replace("Z", "+00:00")) if fields.get("created") else None
        updated_dt = datetime.fromisoformat(fields["updated"].replace("Z", "+00:00")) if fields.get("updated") else None
        duedate_dt = datetime.fromisoformat(fields["duedate"]) if fields.get("duedate") else None

        labels = fields.get("labels", [])
        fix_versions = [v.get("name") for v in fields.get("fixVersions", [])]

        issue_doc = {
            "issueId": issue_id,
            "key": key,
            "summary": summary,
            "sprintId": [sprint_id],
            "boardId": board_id,
            "boardObjectId": board_mongo_id,
            "projectId": str(issue_project_id) if issue_project_id else None,
            "projectKey": issue_project_key,
            "companyName": company_name,
            "storyPoints": float(story_points) if story_points else 0.0,
            "originalEstimateHrs": original_estimate_hrs,
            "timeSpentHrs": time_spent_hrs,
            "assignee": assignee_name,
            "developer": [assignee_name] if assignee_name else [],
            "type": {
                "id": issue_type_data.get("id"),
                "name": issue_type_data.get("name"),
                "description": issue_type_data.get("description")
            },
            "status": {
                "id": status_data.get("id"),
                "name": status_data.get("name")
            },
            "priority": priority_data.get("name") if priority_data else None,
            "issueCreatedAt": created_dt,
            "issueUpdatedAt": updated_dt,
            "duedate": duedate_dt,
            "label": labels,
            "fixVersionNames": fix_versions,
            "updatedAt": datetime.utcnow()
        }
        if company_id:
            issue_doc["companyId"] = company_id

        await tenant_db.sprint_issues.update_one(
            {"issueId": issue_id},
            {
                "$set": issue_doc,
                "$setOnInsert": {"createdAt": datetime.utcnow()}
            },
            upsert=True
        )
        synced_issues_count += 1

    if company_id:
        await tenant_db.sprint_issues.update_many(
            {"$or": [{"companyId": {"$exists": False}}, {"companyId": None}, {"companyId": ""}]},
            {"$set": {"companyId": company_id}}
        )

    return {
        "message": f"Sprint issues synced successfully for sprint {sprint_id}",
        "totalSynced": synced_issues_count
    }


@app.get("/jira/db-sprint-issues/{company_name}")
async def get_db_sprint_issues(company_name: str, sprint_id: Optional[int] = None):
    tenant_db = get_tenant_db(company_name)
    query = {}
    if sprint_id:
        query["sprintId"] = sprint_id

    issues = await tenant_db.sprint_issues.find(query).to_list(None)
    for issue in issues:
        issue["_id"] = str(issue["_id"])

    return {"issues": issues}
