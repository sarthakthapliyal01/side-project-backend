from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import db, meta_db, client, MONGO_URL
import httpx
import re
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta
from typing import Optional
from bson import ObjectId
from models.board import Board
from models.sprint import Sprint, SprintIssue
from models.pull_request import PullRequest

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_tenant_db(company_name: str):
    db_name = company_name.lower().replace(" ", "_").strip()
    return client[db_name]

def to_object_id(val):
    if not val:
        return None
    if isinstance(val, ObjectId):
        return val
    val_str = str(val).strip()
    if ObjectId.is_valid(val_str):
        return ObjectId(val_str)
    return None

def sanitize_doc(doc: dict) -> dict:
    if not doc:
        return doc
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    for field in ["companyId", "projectId", "boardId"]:
        if field in doc and doc[field] is not None:
            doc[field] = str(doc[field])
    if "sprintId" in doc and isinstance(doc["sprintId"], list):
        doc["sprintId"] = [str(sid) if isinstance(sid, ObjectId) else sid for sid in doc["sprintId"]]
    
    # Virtual properties for UI compatibility if reading a PR document
    if "prId" in doc or "prCreatedBy" in doc:
        doc["author"] = doc.get("author") or doc.get("prCreatedBy")
        doc["repoName"] = doc.get("repoName") or doc.get("repo")
        if "reviewer" not in doc or not doc["reviewer"]:
            revs = [r.get("user") for r in doc.get("reviews", []) if r.get("user")]
            doc["reviewer"] = ", ".join(sorted(set(revs))) if revs else "Unassigned"
        if "daysOpen" not in doc or doc.get("daysOpen") is None:
            pr_created = doc.get("prCreatedAt")
            if isinstance(pr_created, datetime):
                doc["daysOpen"] = (datetime.utcnow() - pr_created.replace(tzinfo=None)).days
            else:
                doc["daysOpen"] = 0
    return doc


# ==============================================================================
# 1. GENERAL / SYSTEM APIS
# ==============================================================================

@app.get("/")
def root():
    return {"message": "Backend is working"}

@app.get("/test-db")
async def test_db():
    collections = await db.list_collection_names()
    return {"collections": collections}

@app.post("/users")
async def create_user(user: dict):
    existing_user = await db.users.find_one({"email": user.get("email")})
    if existing_user:
        return {"message": "User already exists"}
    result = await db.users.insert_one(user)
    return {"message": "User added", "id": str(result.inserted_id)}

@app.get("/users")
async def get_users():
    users = []
    async for user in db.users.find():
        user["_id"] = str(user["_id"])
        users.append(user)
    return users

@app.post("/companies")
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

    await meta_db.companies.update_one(
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

    return {
        "message": "Company workspace ready",
        "companyName": company_name,
        "databaseName": database_name
    }

@app.get("/boards/{company_name}")
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
    existing_board = await tenant_db.boards.find_one({"boardId": board.boardId})
    if existing_board:
        return {"message": "Board already exists"}

    board_data = board.model_dump()
    result = await tenant_db.boards.insert_one(board_data)
    return {"message": "Board created", "id": str(result.inserted_id)}


# ==============================================================================
# 2. JIRA INTEGRATION APIS
# ==============================================================================

class JiraConnectionRequest(BaseModel):
    jira_host: str
    jira_email: EmailStr
    jira_token: str

class SaveJiraRequest(BaseModel):
    companyName: str
    jira_host: str
    jira_email: EmailStr
    jira_token: str

class ProjectSelectionRequest(BaseModel):
    projectId: str
    isSelected: bool

@app.post("/jira/test-connection")
async def test_jira_connection(data: JiraConnectionRequest):
    jira_host = data.jira_host.replace(".atlassian.net", "").replace("https://", "")
    url = f"https://{jira_host}.atlassian.net/rest/api/3/project"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(data.jira_email, data.jira_token),
            headers={"Accept": "application/json"}
        )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid Jira credentials")

    projects = response.json()
    return {"connected": True, "project_count": len(projects), "projects": projects}

@app.post("/jira/save-connection")
async def save_jira_connection(data: SaveJiraRequest):
    tenant_db = get_tenant_db(data.companyName)
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
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not connection:
        return {"connected": False}
    return {
        "connected": True,
        "jira_host": connection.get("jira_host"),
        "jira_email": connection.get("jira_email")
    }

@app.get("/jira/boards/{company_name}")
async def get_jira_boards(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not connection:
        raise HTTPException(status_code=404, detail="Jira connection not found")

    jira_host = connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(connection["jira_email"], connection["jira_token"]),
            headers={"Accept": "application/json"}
        )
    return response.json()

# 1. Modular Endpoint: Sync Projects
@app.post("/jira/sync-projects/{company_name}")
async def sync_jira_projects(company_name: str):
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

    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not jira_connection:
        raise HTTPException(status_code=404, detail="Jira connection not found")

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    url = f"https://{jira_host}.atlassian.net/rest/api/3/project"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(
            url,
            auth=(jira_connection["jira_email"], jira_connection["jira_token"]),
            headers={"Accept": "application/json"}
        )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch Jira projects")

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
            set_fields["companyId"] = to_object_id(company_id) or company_id

        await tenant_db.projects.update_one(
            {"projectId": project_id},
            {"$set": set_fields, "$setOnInsert": set_on_insert_fields},
            upsert=True
        )

    return {"message": "Projects synced successfully", "totalProjects": len(jira_projects)}

# 2. Modular Endpoint: Sync Boards
@app.post("/jira/sync-boards/{company_name}")
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
    company_id = to_object_id(company_doc["_id"]) if company_doc and "_id" in company_doc else None

    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not jira_connection:
        return {"message": "Jira connection not found"}

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}

    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"

    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(url, auth=auth, headers=headers)

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch Jira boards")

    boards = response.json().get("values", [])

    for board in boards:
        location = board.get("location", {})
        project_key = location.get("projectKey")
        project_id_str = str(location.get("projectId")) if location.get("projectId") else None

        proj_doc = None
        if project_key or project_id_str:
            proj_doc = await tenant_db.projects.find_one({"$or": [{"projectKey": project_key}, {"projectId": project_id_str}]})
        
        proj_mongo_id = to_object_id(proj_doc["_id"]) if proj_doc else None

        board_data = {
            "boardId": board["id"],
            "boardName": board["name"],
            "boardType": board["type"],
            "boardSelf": board.get("self"),
            "isPrivate": board.get("isPrivate", False),
            "companyId": company_id,
            "projectId": proj_mongo_id,
            "boardLocation": {
                "projectId": project_id_str,
                "projectName": location.get("projectName"),
                "projectKey": project_key,
                "projectTypeKey": location.get("projectTypeKey"),
                "avatarURI": location.get("avatarURI"),
                "displayName": location.get("displayName"),
                "name": location.get("name"),
                "githubProjectV2NodeId": None,
                "githubResourceKind": None
            }
        }

        await tenant_db.boards.update_one(
            {"boardId": board["id"]},
            {"$set": board_data},
            upsert=True
        )

    return {"message": "Boards synced successfully", "totalBoards": len(boards)}

# 3. Modular Endpoint: Sync Sprints
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
    company_id = to_object_id(company_doc["_id"]) if company_doc and "_id" in company_doc else None

    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not jira_connection:
        raise HTTPException(status_code=404, detail="Jira connection not found")

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
            b_res = await httpx_client.get(f"https://{jira_host}.atlassian.net/rest/agile/1.0/board", auth=auth, headers=headers)
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
            board_mongo_id = to_object_id(board_doc["_id"]) if board_doc else None
            project_key = board_doc.get("boardLocation", {}).get("projectKey") if board_doc else None
            
            proj_doc = None
            if project_key:
                proj_doc = await tenant_db.projects.find_one({"projectKey": project_key})
            project_mongo_id = to_object_id(proj_doc["_id"]) if proj_doc else None

            sprint_values = response.json().get("values", [])
            for s in sprint_values:
                sprint_id = s.get("id")
                start_dt = datetime.fromisoformat(s["startDate"].replace("Z", "+00:00")) if s.get("startDate") else None
                end_dt = datetime.fromisoformat(s["endDate"].replace("Z", "+00:00")) if s.get("endDate") else None
                complete_dt = datetime.fromisoformat(s["completeDate"].replace("Z", "+00:00")) if s.get("completeDate") else None

                total_days = (end_dt - start_dt).total_seconds() / 86400.0 if start_dt and end_dt else None

                sprint_doc = {
                    "sprintId": sprint_id,
                    "name": s.get("name"),
                    "state": s.get("state"),
                    "boardId": board_mongo_id,
                    "boardObjectId": str(board_mongo_id) if board_mongo_id else None,
                    "originBoardId": s.get("originBoardId"),
                    "projectId": project_mongo_id,
                    "projectKey": project_key,
                    "companyId": company_id,
                    "companyName": company_name,
                    "startDate": start_dt,
                    "endDate": end_dt,
                    "completeDate": complete_dt,
                    "totalDays": total_days,
                    "updatedAt": datetime.utcnow()
                }

                await tenant_db.sprints.update_one(
                    {"sprintId": sprint_id},
                    {"$set": sprint_doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
                    upsert=True
                )
                synced_sprints_count += 1

    return {"message": "Sprints synced successfully", "totalSynced": synced_sprints_count}

# 4. Modular Endpoint: Sync Sprint Issues
@app.post("/jira/sync-sprint-issues/{company_name}/{sprint_id}")
async def sync_jira_sprint_issues(company_name: str, sprint_id: str):
    tenant_db = get_tenant_db(company_name)
    company_doc = await meta_db.companies.find_one({"companyName": company_name})
    if not company_doc:
        db_name = company_name.lower().replace(" ", "_").strip()
        company_doc = await meta_db.companies.find_one({"databaseName": db_name})
    if not company_doc:
        company_doc = await meta_db.companies.find_one({
            "companyName": {"$regex": f"^{re.escape(company_name)}$", "$options": "i"}
        })
    company_id = to_object_id(company_doc["_id"]) if company_doc and "_id" in company_doc else None

    possible_s_ids = [sprint_id]
    if str(sprint_id).isdigit():
        possible_s_ids.append(int(sprint_id))

    sprint_doc = await tenant_db.sprints.find_one({"$or": [{"sprintId": {"$in": possible_s_ids}}, {"name": sprint_id}]})
    sprint_mongo_id = to_object_id(sprint_doc["_id"]) if sprint_doc else None
    board_mongo_id = to_object_id(sprint_doc.get("boardId")) if sprint_doc else None
    project_mongo_id = to_object_id(sprint_doc.get("projectId")) if sprint_doc else None
    project_key = sprint_doc.get("projectKey") if sprint_doc else None

    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not jira_connection:
        raise HTTPException(status_code=404, detail="Jira connection not found")

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}

    url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{sprint_id}/issue"
    async with httpx.AsyncClient() as httpx_client:
        response = await httpx_client.get(url, auth=auth, headers=headers)

    if response.status_code != 200:
        # Fallback if sprint_id in Jira endpoint requires integer
        if not str(sprint_id).isdigit() and sprint_doc and sprint_doc.get("sprintId"):
            alt_sid = sprint_doc.get("sprintId")
            url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{alt_sid}/issue"
            async with httpx.AsyncClient() as httpx_client:
                response = await httpx_client.get(url, auth=auth, headers=headers)

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to fetch issues for sprint {sprint_id}")

    issues_data = response.json().get("issues", [])
    synced_issues_count = 0

    for issue in issues_data:
        fields = issue.get("fields", {})
        issue_id = int(issue.get("id"))
        key = issue.get("key")
        summary = fields.get("summary", "")

        issue_project = fields.get("project", {})
        issue_project_key = issue_project.get("key") or project_key

        story_points = 0.0
        for cf_key in ["customfield_10016", "customfield_10026", "customfield_10002", "customfield_10004", "customfield_10024", "customfield_10028", "customfield_10030", "storyPoints"]:
            if fields.get(cf_key) is not None:
                try:
                    story_points = float(fields.get(cf_key))
                    break
                except (ValueError, TypeError):
                    pass

        timetracking = fields.get("timetracking", {})
        orig_sec = (
            fields.get("timeoriginalestimate") or
            fields.get("aggregatetimeoriginalestimate") or
            timetracking.get("originalEstimateSeconds") or
            0
        )
        spent_sec = (
            fields.get("timespent") or
            fields.get("aggregatetimespent") or
            timetracking.get("timeSpentSeconds") or
            0
        )

        original_estimate_hrs = float(orig_sec) / 3600.0 if orig_sec else 0.0
        time_spent_hrs = float(spent_sec) / 3600.0 if spent_sec else 0.0

        if original_estimate_hrs == 0.0 and story_points > 0:
            original_estimate_hrs = story_points * 8.0

        assignee_data = fields.get("assignee")
        assignee_name = assignee_data.get("displayName") if assignee_data else None

        issue_type_data = fields.get("issuetype", {})
        status_data = fields.get("status", {})
        priority_data = fields.get("priority", {})

        created_dt = parse_dt(fields.get("created"))
        updated_dt = parse_dt(fields.get("updated"))
        duedate_dt = parse_dt(fields.get("duedate"))

        labels = fields.get("labels", [])
        fix_versions = [v.get("name") for v in fields.get("fixVersions", [])]

        sprint_id_list = []
        if sprint_mongo_id:
            sprint_id_list.append(sprint_mongo_id)
        if sprint_id is not None:
            sprint_id_list.append(sprint_id)
            sprint_id_list.append(str(sprint_id))
            if str(sprint_id).isdigit():
                sprint_id_list.append(int(sprint_id))

        issue_doc = {
            "issueId": issue_id,
            "key": key,
            "summary": summary,
            "sprintId": sprint_id_list,
            "boardId": board_mongo_id,
            "projectId": project_mongo_id,
            "projectKey": issue_project_key,
            "companyId": company_id,
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

        await tenant_db.sprint_issues.update_one(
            {"issueId": issue_id},
            {"$set": issue_doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
            upsert=True
        )
        synced_issues_count += 1

    return {
        "message": f"Sprint issues synced successfully for sprint {sprint_id}",
        "totalSynced": synced_issues_count
    }

# ORCHESTRATED UNIFIED SYNC: Calls sync_jira_projects, sync_jira_boards, sync_jira_sprints, & sync_jira_sprint_issues sequentially
@app.post("/jira/sync-all/{company_name}")
@app.post("/sync-all/{company_name}")
async def sync_all_jira_data(company_name: str):
    tenant_db = get_tenant_db(company_name)

    jira_conn = await tenant_db.connections.find_one({"integrationType": "jira"})
    github_conn = await tenant_db.connections.find_one({"integrationType": "github"})

    if not jira_conn and not github_conn:
        return {
            "message": "Nothing to sync: Neither Jira nor GitHub integration is connected.",
            "synced": False,
            "connected": False
        }

    proj_res = {}
    boards_res = {}
    sprints_res = {}
    total_issues = 0

    if jira_conn:
        try:
            # 1. Sync Projects
            proj_res = await sync_jira_projects(company_name)

            # 2. Sync Boards
            boards_res = await sync_jira_boards(company_name)

            # 3. Sync Sprints
            sprints_res = await sync_jira_sprints(company_name)

            # 4. Sync Sprint Issues for each synced sprint
            sprints = await tenant_db.sprints.find().to_list(None)
            for sprint in sprints:
                sid = sprint.get("sprintId") or sprint.get("id")
                if sid is not None and str(sid).strip():
                    try:
                        iss_res = await sync_jira_sprint_issues(company_name, str(sid))
                        total_issues += iss_res.get("totalSynced", 0)
                    except Exception as e:
                        print(f"Error syncing issues for sprint {sid}: {e}")
        except Exception as e:
            print(f"Error syncing Jira data: {e}")

    if github_conn:
        # 5. Sync GitHub Repos & PRs if GitHub integration exists
        try:
            await sync_all_github_data(company_name)
        except Exception as e:
            print(f"Error syncing GitHub data in sync-all: {e}")

    return {
        "message": "Data synced successfully",
        "totalProjects": proj_res.get("totalProjects", 0),
        "totalBoards": boards_res.get("totalBoards", 0),
        "totalSprints": sprints_res.get("totalSynced", 0),
        "totalSprintIssues": total_issues
    }

@app.get("/jira/projects/{company_name}")
async def get_jira_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)
    projects = await tenant_db.projects.find().to_list(None)
    projects_list = [sanitize_doc(project) for project in projects]
    return {"totalProjects": len(projects_list), "projects": projects_list}

@app.put("/jira/project-selection/{company_name}")
async def update_project_selection(company_name: str, data: ProjectSelectionRequest):
    tenant_db = get_tenant_db(company_name)
    result = await tenant_db.projects.update_one(
        {"projectId": data.projectId},
        {"$set": {"isSelected": data.isSelected, "updatedAt": datetime.utcnow()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"message": "Project selection updated"}

@app.get("/jira/selected-projects/{company_name}")
async def get_selected_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)
    projects = await tenant_db.projects.find({"isSelected": True}).to_list(length=None)
    projects_list = [sanitize_doc(project) for project in projects]
    return {"projects": projects_list}

@app.get("/jira/sprints/{company_name}")
async def get_jira_sprints(company_name: str, project_id: str = None):
    tenant_db = get_tenant_db(company_name)
    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not jira_connection:
        return {"sprints": []}

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}
    all_sprints = []
    seen_sprint_ids = set()

    try:
        async with httpx.AsyncClient() as httpx_client:
            boards_url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board"
            if project_id:
                boards_url += f"?projectKeyOrId={project_id}"

            boards_res = await httpx_client.get(boards_url, auth=auth, headers=headers, timeout=10.0)
            if boards_res.status_code == 200:
                boards = boards_res.json().get("values", [])
                for board in boards:
                    board_id = board.get("id")
                    if not board_id:
                        continue
                    sprints_res = await httpx_client.get(
                        f"https://{jira_host}.atlassian.net/rest/agile/1.0/board/{board_id}/sprint",
                        auth=auth, headers=headers, timeout=10.0
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

@app.get("/jira/db-sprints/{company_name}")
async def get_db_sprints(company_name: str, board_id: Optional[str] = None, project_id: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)
    query = {}
    target_pid = project_id or board_id
    if target_pid:
        possible_ids = [target_pid]
        if str(target_pid).isdigit():
            possible_ids.append(int(target_pid))
        obj_id = to_object_id(target_pid)
        if obj_id:
            possible_ids.append(obj_id)

        or_proj_lookup = [
            {"projectId": str(target_pid)},
            {"projectKey": str(target_pid)},
            {"projectName": str(target_pid)}
        ]
        if str(target_pid).isdigit():
            or_proj_lookup.append({"projectId": int(target_pid)})

        proj_doc = await tenant_db.projects.find_one({"$or": or_proj_lookup})
        project_keys = [str(target_pid)]
        if proj_doc:
            if "_id" in proj_doc:
                possible_ids.append(proj_doc["_id"])
                possible_ids.append(str(proj_doc["_id"]))
            if proj_doc.get("projectId"):
                possible_ids.append(proj_doc["projectId"])
                possible_ids.append(str(proj_doc["projectId"]))
                if str(proj_doc["projectId"]).isdigit():
                    possible_ids.append(int(proj_doc["projectId"]))
            if proj_doc.get("projectKey"):
                project_keys.append(proj_doc["projectKey"])

        query["$or"] = [
            {"projectId": {"$in": possible_ids}},
            {"projectKey": {"$in": project_keys}},
            {"boardId": {"$in": possible_ids}},
            {"originBoardId": {"$in": possible_ids}}
        ]

    sprints = await tenant_db.sprints.find(query).to_list(None)
    sprints_list = [sanitize_doc(s) for s in sprints]
    return {"sprints": sprints_list}

@app.get("/jira/db-sprint-issues/{company_name}")
async def get_db_sprint_issues(company_name: str, sprint_id: Optional[str] = None, project_id: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)
    and_conditions = []

    if sprint_id:
        possible_sprint_ids = [sprint_id]
        if str(sprint_id).isdigit():
            possible_sprint_ids.append(int(sprint_id))
        obj_s = to_object_id(sprint_id)
        if obj_s:
            possible_sprint_ids.append(obj_s)

        or_conditions = [
            {"sprintId": int(sprint_id) if str(sprint_id).isdigit() else sprint_id},
            {"sprintId": str(sprint_id)},
            {"name": sprint_id}
        ]
        if obj_s:
            or_conditions.append({"_id": obj_s})

        sprint_doc = await tenant_db.sprints.find_one({"$or": or_conditions})
        if sprint_doc:
            if "_id" in sprint_doc:
                possible_sprint_ids.append(sprint_doc["_id"])
                possible_sprint_ids.append(str(sprint_doc["_id"]))
            if "sprintId" in sprint_doc:
                possible_sprint_ids.append(sprint_doc["sprintId"])
                possible_sprint_ids.append(str(sprint_doc["sprintId"]))
                try:
                    possible_sprint_ids.append(int(sprint_doc["sprintId"]))
                except (ValueError, TypeError):
                    pass

        and_conditions.append({"sprintId": {"$in": possible_sprint_ids}})

    if project_id:
        possible_proj_ids = [project_id]
        if str(project_id).isdigit():
            possible_proj_ids.append(int(project_id))
        obj_p = to_object_id(project_id)
        if obj_p:
            possible_proj_ids.append(obj_p)

        or_proj_lookup = [
            {"projectId": str(project_id)},
            {"projectKey": str(project_id)},
            {"projectName": str(project_id)}
        ]
        if str(project_id).isdigit():
            or_proj_lookup.append({"projectId": int(project_id)})

        proj_doc = await tenant_db.projects.find_one({"$or": or_proj_lookup})
        project_keys = [str(project_id)]

        if proj_doc:
            if "_id" in proj_doc:
                possible_proj_ids.append(proj_doc["_id"])
                possible_proj_ids.append(str(proj_doc["_id"]))
            if proj_doc.get("projectId"):
                possible_proj_ids.append(proj_doc["projectId"])
                possible_proj_ids.append(str(proj_doc["projectId"]))
                if str(proj_doc["projectId"]).isdigit():
                    possible_proj_ids.append(int(proj_doc["projectId"]))
            if proj_doc.get("projectKey"):
                project_keys.append(proj_doc["projectKey"])

        and_conditions.append({
            "$or": [
                {"projectId": {"$in": possible_proj_ids}},
                {"projectKey": {"$in": project_keys}}
            ]
        })

    query = {}
    if and_conditions:
        if len(and_conditions) == 1:
            query = and_conditions[0]
        else:
            query = {"$and": and_conditions}

    issues = await tenant_db.sprint_issues.find(query).to_list(None)

    issues_list = [sanitize_doc(issue) for issue in issues]
    return {"issues": issues_list}


def parse_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        try:
            return datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val_str, fmt)
            except Exception:
                pass
    return None


@app.get("/jira/burndown/{company_name}")
async def get_jira_burndown(company_name: str, sprint_id: Optional[str] = None, project_id: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)

    sprint_doc = None
    if sprint_id:
        or_conditions = [
            {"sprintId": int(sprint_id) if str(sprint_id).isdigit() else sprint_id},
            {"sprintId": str(sprint_id)},
            {"name": sprint_id},
            {"name": {"$regex": f"^{re.escape(str(sprint_id))}$", "$options": "i"}}
        ]
        obj_s = to_object_id(sprint_id)
        if obj_s:
            or_conditions.append({"_id": obj_s})
        sprint_doc = await tenant_db.sprints.find_one({"$or": or_conditions})
    
    if not sprint_doc:
        query = {}
        if project_id:
            possible_pids = [project_id]
            if str(project_id).isdigit():
                possible_pids.append(int(project_id))
            query["$or"] = [{"projectId": {"$in": possible_pids}}, {"projectKey": project_id}]
        sprint_doc = await tenant_db.sprints.find_one(query, sort=[("createdAt", -1)])

    issues = []
    possible_sprint_ids = []
    if sprint_doc:
        sid = sprint_doc.get("sprintId")
        if sid:
            possible_sprint_ids.extend([sid, str(sid)])
            if str(sid).isdigit():
                possible_sprint_ids.append(int(sid))
        if "_id" in sprint_doc:
            possible_sprint_ids.extend([sprint_doc["_id"], str(sprint_doc["_id"])])

    if possible_sprint_ids:
        issues = await tenant_db.sprint_issues.find({"sprintId": {"$in": possible_sprint_ids}}).to_list(None)
    elif sprint_id or project_id:
        issues_res = await get_db_sprint_issues(company_name, sprint_id=sprint_id, project_id=project_id)
        issues = issues_res.get("issues", [])

    start_dt = parse_dt(sprint_doc.get("startDate")) if sprint_doc else None
    end_dt = parse_dt(sprint_doc.get("endDate")) if sprint_doc else None

    if not start_dt:
        start_dt = datetime.utcnow() - timedelta(days=5)
    if not end_dt:
        end_dt = start_dt + timedelta(days=10)

    num_days = max(2, (end_dt.date() - start_dt.date()).days + 1)
    dates_list = [start_dt.date() + timedelta(days=i) for i in range(num_days)]
    date_labels = [d.strftime("%m/%d") for d in dates_list]
    is_weekend_list = [d.weekday() in (5, 6) for d in dates_list]

    working_days_count = sum(1 for is_w in is_weekend_list if not is_w)
    if working_days_count == 0:
        working_days_count = num_days

    total_sp = sum(float(i.get("storyPoints") or 0) for i in issues)
    total_hrs = sum(
        float(i.get("originalEstimateHrs") or i.get("timeSpentHrs") or (float(i.get("storyPoints") or 0) * 8.0))
        for i in issues
    )

    ideal_sp_daily = total_sp / max(1, working_days_count)
    ideal_hrs_daily = total_hrs / max(1, working_days_count)

    ideal_sp = []
    ideal_hrs = []
    curr_sp = total_sp
    curr_hrs = total_hrs

    for idx, d in enumerate(dates_list):
        if idx == 0:
            ideal_sp.append(round(curr_sp, 1))
            ideal_hrs.append(round(curr_hrs, 1))
        elif idx == num_days - 1:
            ideal_sp.append(0.0)
            ideal_hrs.append(0.0)
        else:
            prev_date = dates_list[idx - 1]
            if prev_date.weekday() not in (5, 6):
                curr_sp = max(0.0, curr_sp - ideal_sp_daily)
                curr_hrs = max(0.0, curr_hrs - ideal_hrs_daily)
            ideal_sp.append(round(max(0.0, curr_sp), 1))
            ideal_hrs.append(round(max(0.0, curr_hrs), 1))

    today_date = datetime.utcnow().date()
    daily_closed_sp = {d: 0.0 for d in dates_list}
    daily_closed_hrs = {d: 0.0 for d in dates_list}

    for issue in issues:
        st = issue.get("status")
        st_name = (st.get("name") if isinstance(st, dict) else str(st or "")).lower()
        is_closed = any(kw in st_name for kw in ["close", "done", "resolve", "complete"])
        
        if is_closed:
            sp_val = float(issue.get("storyPoints") or 0)
            hrs_val = float(issue.get("originalEstimateHrs") or issue.get("timeSpentHrs") or (sp_val * 8.0))
            
            comp_dt = parse_dt(issue.get("workCompletedAt") or issue.get("issueUpdatedAt") or issue.get("updatedAt"))
            c_date = comp_dt.date() if comp_dt else start_dt.date()
            if c_date < dates_list[0]:
                c_date = dates_list[0]
            elif c_date > dates_list[-1]:
                c_date = dates_list[-1]
            
            daily_closed_sp[c_date] = daily_closed_sp.get(c_date, 0.0) + sp_val
            daily_closed_hrs[c_date] = daily_closed_hrs.get(c_date, 0.0) + hrs_val

    actual_sp = []
    actual_hrs = []
    cum_closed_sp = 0.0
    cum_closed_hrs = 0.0

    for d in dates_list:
        if d <= today_date:
            cum_closed_sp += daily_closed_sp.get(d, 0.0)
            cum_closed_hrs += daily_closed_hrs.get(d, 0.0)
            actual_sp.append(round(max(0.0, total_sp - cum_closed_sp), 1))
            actual_hrs.append(round(max(0.0, total_hrs - cum_closed_hrs), 1))
        else:
            actual_sp.append(None)
            actual_hrs.append(None)

    last_5_sprints = await tenant_db.sprints.find().sort("createdAt", -1).limit(5).to_list(None)
    five_spts_sp_sum = sum(float(s.get("totalStoryPoints") or 0) for s in last_5_sprints)
    five_spts_sp_avg = round(five_spts_sp_sum / max(1, len(last_5_sprints)), 1)
    five_spts_hrs_avg = round(five_spts_sp_avg * 8, 1)

    yesterday_date = today_date - timedelta(days=1)

    return {
        "sprintName": sprint_doc.get("name", "Current Sprint") if sprint_doc else "Current Sprint",
        "startDate": start_dt.isoformat(),
        "endDate": end_dt.isoformat(),
        "dates": date_labels,
        "isWeekend": is_weekend_list,
        "sp": {
            "todaysBurned": round(daily_closed_sp.get(today_date, 0.0), 1),
            "yesterdaysBurned": round(daily_closed_sp.get(yesterday_date, 0.0), 1),
            "target": round(ideal_sp_daily, 1),
            "compleTT": round(cum_closed_sp, 1),
            "fiveSptsAvg": five_spts_sp_avg,
            "total": round(total_sp, 1),
            "ideal": ideal_sp,
            "actual": actual_sp,
        },
        "hrs": {
            "todaysBurned": round(daily_closed_hrs.get(today_date, 0.0), 1),
            "yesterdaysBurned": round(daily_closed_hrs.get(yesterday_date, 0.0), 1),
            "target": round(ideal_hrs_daily, 1),
            "compleTT": round(cum_closed_hrs, 1),
            "fiveSptsAvg": five_spts_hrs_avg,
            "total": round(total_hrs, 1),
            "ideal": ideal_hrs,
            "actual": actual_hrs,
        }
    }


@app.get("/jira/burnup/{company_name}")
async def get_jira_burnup(company_name: str, sprint_id: Optional[str] = None, project_id: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)

    sprint_doc = None
    if sprint_id:
        or_conditions = [
            {"sprintId": int(sprint_id) if str(sprint_id).isdigit() else sprint_id},
            {"sprintId": str(sprint_id)},
            {"name": sprint_id},
            {"name": {"$regex": f"^{re.escape(str(sprint_id))}$", "$options": "i"}}
        ]
        obj_s = to_object_id(sprint_id)
        if obj_s:
            or_conditions.append({"_id": obj_s})
        sprint_doc = await tenant_db.sprints.find_one({"$or": or_conditions})
    
    if not sprint_doc:
        query = {}
        if project_id:
            possible_pids = [project_id]
            if str(project_id).isdigit():
                possible_pids.append(int(project_id))
            query["$or"] = [{"projectId": {"$in": possible_pids}}, {"projectKey": project_id}]
        sprint_doc = await tenant_db.sprints.find_one(query, sort=[("createdAt", -1)])

    issues = []
    possible_sprint_ids = []
    if sprint_doc:
        sid = sprint_doc.get("sprintId")
        if sid:
            possible_sprint_ids.extend([sid, str(sid)])
            if str(sid).isdigit():
                possible_sprint_ids.append(int(sid))
        if "_id" in sprint_doc:
            possible_sprint_ids.extend([sprint_doc["_id"], str(sprint_doc["_id"])])

    if possible_sprint_ids:
        issues = await tenant_db.sprint_issues.find({"sprintId": {"$in": possible_sprint_ids}}).to_list(None)
    elif sprint_id or project_id:
        issues_res = await get_db_sprint_issues(company_name, sprint_id=sprint_id, project_id=project_id)
        issues = issues_res.get("issues", [])

    start_dt = parse_dt(sprint_doc.get("startDate")) if sprint_doc else None
    end_dt = parse_dt(sprint_doc.get("endDate")) if sprint_doc else None

    if not start_dt:
        start_dt = datetime.utcnow() - timedelta(days=5)
    if not end_dt:
        end_dt = start_dt + timedelta(days=10)

    num_days = max(2, (end_dt.date() - start_dt.date()).days + 1)
    dates_list = [start_dt.date() + timedelta(days=i) for i in range(num_days)]
    date_labels = [d.strftime("%m/%d") for d in dates_list]
    is_weekend_list = [d.weekday() in (5, 6) for d in dates_list]

    working_days_count = sum(1 for is_w in is_weekend_list if not is_w)
    if working_days_count == 0:
        working_days_count = num_days

    total_sp = sum(float(i.get("storyPoints") or 0) for i in issues)
    total_hrs = sum(
        float(i.get("originalEstimateHrs") or i.get("timeSpentHrs") or (float(i.get("storyPoints") or 0) * 8.0))
        for i in issues
    )

    ideal_sp_daily = total_sp / max(1, working_days_count)
    ideal_hrs_daily = total_hrs / max(1, working_days_count)

    ideal_sp = []
    ideal_hrs = []
    curr_sp = 0.0
    curr_hrs = 0.0

    for idx, d in enumerate(dates_list):
        if idx == 0:
            ideal_sp.append(0.0)
            ideal_hrs.append(0.0)
        elif idx == num_days - 1:
            ideal_sp.append(round(total_sp, 1))
            ideal_hrs.append(round(total_hrs, 1))
        else:
            prev_date = dates_list[idx - 1]
            if prev_date.weekday() not in (5, 6):
                curr_sp = min(total_sp, curr_sp + ideal_sp_daily)
                curr_hrs = min(total_hrs, curr_hrs + ideal_hrs_daily)
            ideal_sp.append(round(min(total_sp, curr_sp), 1))
            ideal_hrs.append(round(min(total_hrs, curr_hrs), 1))

    today_date = datetime.utcnow().date()
    daily_closed_sp = {d: 0.0 for d in dates_list}
    daily_closed_hrs = {d: 0.0 for d in dates_list}

    for issue in issues:
        st = issue.get("status")
        st_name = (st.get("name") if isinstance(st, dict) else str(st or "")).lower()
        is_closed = any(kw in st_name for kw in ["close", "done", "resolve", "complete"])
        
        if is_closed:
            sp_val = float(issue.get("storyPoints") or 0)
            hrs_val = float(issue.get("originalEstimateHrs") or issue.get("timeSpentHrs") or (sp_val * 8.0))
            
            comp_dt = parse_dt(issue.get("workCompletedAt") or issue.get("issueUpdatedAt") or issue.get("updatedAt"))
            c_date = comp_dt.date() if comp_dt else start_dt.date()
            if c_date < dates_list[0]:
                c_date = dates_list[0]
            elif c_date > dates_list[-1]:
                c_date = dates_list[-1]
            
            daily_closed_sp[c_date] = daily_closed_sp.get(c_date, 0.0) + sp_val
            daily_closed_hrs[c_date] = daily_closed_hrs.get(c_date, 0.0) + hrs_val

    actual_sp = []
    actual_hrs = []
    cum_closed_sp = 0.0
    cum_closed_hrs = 0.0

    for d in dates_list:
        if d <= today_date:
            cum_closed_sp += daily_closed_sp.get(d, 0.0)
            cum_closed_hrs += daily_closed_hrs.get(d, 0.0)
            actual_sp.append(round(min(total_sp, cum_closed_sp), 1))
            actual_hrs.append(round(min(total_hrs, cum_closed_hrs), 1))
        else:
            actual_sp.append(None)
            actual_hrs.append(None)

    last_5_sprints = await tenant_db.sprints.find().sort("createdAt", -1).limit(5).to_list(None)
    five_spts_sp_sum = sum(float(s.get("totalStoryPoints") or 0) for s in last_5_sprints)
    five_spts_sp_avg = round(five_spts_sp_sum / max(1, len(last_5_sprints)), 1)
    five_spts_hrs_avg = round(five_spts_sp_avg * 8, 1)

    yesterday_date = today_date - timedelta(days=1)

    return {
        "sprintName": sprint_doc.get("name", "Current Sprint") if sprint_doc else "Current Sprint",
        "startDate": start_dt.isoformat(),
        "endDate": end_dt.isoformat(),
        "dates": date_labels,
        "isWeekend": is_weekend_list,
        "sp": {
            "todaysBurned": round(daily_closed_sp.get(today_date, 0.0), 1),
            "yesterdaysBurned": round(daily_closed_sp.get(yesterday_date, 0.0), 1),
            "target": round(ideal_sp_daily, 1),
            "compleTT": round(cum_closed_sp, 1),
            "fiveSptsAvg": five_spts_sp_avg,
            "total": round(total_sp, 1),
            "ideal": ideal_sp,
            "actual": actual_sp,
        },
        "hrs": {
            "todaysBurned": round(daily_closed_hrs.get(today_date, 0.0), 1),
            "yesterdaysBurned": round(daily_closed_hrs.get(yesterday_date, 0.0), 1),
            "target": round(ideal_hrs_daily, 1),
            "compleTT": round(cum_closed_hrs, 1),
            "fiveSptsAvg": five_spts_hrs_avg,
            "total": round(total_hrs, 1),
            "ideal": ideal_hrs,
            "actual": actual_hrs,
        }
    }


# ==============================================================================
# 3. GITHUB INTEGRATION APIS
# ==============================================================================

class GitHubConnectionRequest(BaseModel):
    github_owner: str
    github_token: str

class SaveGitHubRequest(BaseModel):
    companyName: str
    github_owner: str
    github_token: str

@app.post("/github/test-connection")
async def test_github_connection(data: GitHubConnectionRequest):
    headers = {
        "Authorization": f"Bearer {data.github_token}",
        "Accept": "application/vnd.github+json"
    }
    async with httpx.AsyncClient() as httpx_client:
        # First check authenticated user
        user_res = await httpx_client.get("https://api.github.com/user", headers=headers)
        if user_res.status_code == 200:
            user_data = user_res.json()
            return {
                "connected": True,
                "owner": user_data.get("login"),
                "type": user_data.get("type", "User")
            }
        
        # Next check users endpoint
        u_res = await httpx_client.get(f"https://api.github.com/users/{data.github_owner}", headers=headers)
        if u_res.status_code == 200:
            u_data = u_res.json()
            return {
                "connected": True,
                "owner": u_data.get("login"),
                "type": u_data.get("type")
            }

        # Next check orgs endpoint
        org_res = await httpx_client.get(f"https://api.github.com/orgs/{data.github_owner}", headers=headers)
        if org_res.status_code == 200:
            org_data = org_res.json()
            return {
                "connected": True,
                "owner": org_data.get("login"),
                "type": "Organization"
            }

    raise HTTPException(
        status_code=400,
        detail="Invalid GitHub owner or Personal Access Token"
    )

@app.post("/github/save-connection")
async def save_github_connection(data: SaveGitHubRequest):
    tenant_db = get_tenant_db(data.companyName)
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

    return {"message": "GitHub connection saved successfully"}

@app.get("/github/connection/{company_name}")
async def get_github_connection(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "github"})
    if not connection:
        return {"connected": False}
    return {
        "connected": True,
        "github_owner": connection.get("github_owner")
    }

async def fetch_github_repos_from_api(owner: str, token: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    all_repos = []
    seen_ids = set()

    async with httpx.AsyncClient() as httpx_client:
        # 1. Fetch repos for authenticated user (includes private, internal, and org repos token has access to)
        user_url = "https://api.github.com/user/repos?per_page=100&type=all&sort=updated"
        res = await httpx_client.get(user_url, headers=headers)
        if res.status_code == 200:
            for r in res.json():
                r_id = str(r.get("id"))
                if r_id not in seen_ids:
                    seen_ids.add(r_id)
                    all_repos.append(r)

        # 2. Try org repos if owner is specified
        if owner:
            org_url = f"https://api.github.com/orgs/{owner}/repos?per_page=100&type=all&sort=updated"
            res_org = await httpx_client.get(org_url, headers=headers)
            if res_org.status_code == 200:
                for r in res_org.json():
                    r_id = str(r.get("id"))
                    if r_id not in seen_ids:
                        seen_ids.add(r_id)
                        all_repos.append(r)

        # 3. Try users endpoint for public repos of owner
        if owner:
            users_url = f"https://api.github.com/users/{owner}/repos?per_page=100&sort=updated"
            res_user = await httpx_client.get(users_url, headers=headers)
            if res_user.status_code == 200:
                for r in res_user.json():
                    r_id = str(r.get("id"))
                    if r_id not in seen_ids:
                        seen_ids.add(r_id)
                        all_repos.append(r)

    if owner and all_repos:
        owner_clean = owner.strip().lower()
        matched = [
            r for r in all_repos
            if (r.get("owner", {}).get("login", "").lower() == owner_clean) or
               (r.get("full_name", "").lower().startswith(f"{owner_clean}/"))
        ]
        if matched:
            return matched

    return all_repos

@app.get("/github/repos/{company_name}")
async def get_github_repos(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "github"})
    if not connection:
        return {"connected": False, "repos": []}

    owner = connection.get("github_owner", "")
    token = connection.get("github_token", "")

    repos_data = await fetch_github_repos_from_api(owner, token)

    repos = [
        {
            "id": str(r.get("id")),
            "name": r.get("name"),
            "full_name": r.get("full_name"),
            "html_url": r.get("html_url"),
            "description": r.get("description"),
            "is_private": r.get("private", False)
        }
        for r in repos_data
    ]
    return {"connected": True, "repos": repos}

@app.post("/github/sync-repos/{company_name}")
async def sync_github_repos(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "github"})
    if not connection:
        raise HTTPException(status_code=404, detail="GitHub connection not found")

    owner = connection.get("github_owner", "")
    token = connection.get("github_token", "")

    repos_data = await fetch_github_repos_from_api(owner, token)
    if not repos_data:
        return {"message": "No GitHub repositories found for this account", "totalSynced": 0}

    synced_count = 0
    for r in repos_data:
        r_owner = r.get("owner", {}).get("login") or owner
        repo_doc = {
            "repoId": str(r.get("id")),
            "name": r.get("name"),
            "fullName": r.get("full_name"),
            "owner": r_owner,
            "htmlUrl": r.get("html_url"),
            "description": r.get("description"),
            "isPrivate": r.get("private", False),
            "updatedAt": datetime.utcnow()
        }
        await tenant_db.github_repos.update_one(
            {"repoId": str(r.get("id"))},
            {"$set": repo_doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
            upsert=True
        )
        synced_count += 1

    return {"message": "GitHub repositories synced successfully", "totalSynced": synced_count}

@app.post("/github/sync-all/{company_name}")
async def sync_all_github_data(company_name: str):
    repos_res = await sync_github_repos(company_name)
    prs_res = await sync_github_prs(company_name)
    return {
        "message": "GitHub repositories and PRs synced successfully",
        "totalReposSynced": repos_res.get("totalSynced", 0),
        "totalPRsSynced": prs_res.get("totalSynced", 0)
    }

@app.get("/github/db-repos/{company_name}")
async def get_db_github_repos(company_name: str):
    tenant_db = get_tenant_db(company_name)
    repos = await tenant_db.github_repos.find().to_list(None)

    # Fallback to live GitHub API if DB repos is empty
    if not repos:
        live_res = await get_github_repos(company_name)
        live_repos = live_res.get("repos", [])
        return {"repos": live_repos}

    for r in repos:
        r["_id"] = str(r["_id"])
    return {"repos": repos}


@app.get("/github/prs/{company_name}")
async def get_github_prs(company_name: str, repo_name: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)
    query = {}
    if repo_name and repo_name != "All repositories":
        query["$or"] = [{"repo": repo_name}, {"repoName": repo_name}]

    prs = await tenant_db.github_prs.find(query).to_list(None)
    prs_list = [sanitize_doc(p) for p in prs]

    total = len(prs_list)
    open_prs = sum(1 for p in prs_list if str(p.get("status")).lower() == "open")
    merged_prs = sum(1 for p in prs_list if str(p.get("merged")).lower() in ["yes", "true", "merged"])
    closed_no_merge = sum(1 for p in prs_list if str(p.get("status")).lower() == "closed" and str(p.get("merged")).lower() in ["no", "false"])
    reviewed = sum(1 for p in prs_list if str(p.get("status")).lower() == "open" and p.get("reviewer") and p.get("reviewer") != "Unassigned")
    unreviewed = open_prs - reviewed

    merge_durations_seconds = []
    for p in prs_list:
        is_merged = str(p.get("merged")).lower() in ["yes", "true", "merged"]
        created_at = p.get("prCreatedAt") or p.get("createdAt")
        merged_at = p.get("prMergedAt")

        if is_merged and created_at and merged_at:
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except Exception:
                    created_at = None
            if isinstance(merged_at, str):
                try:
                    merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                except Exception:
                    merged_at = None

            if created_at and merged_at:
                c_dt = created_at.replace(tzinfo=None) if hasattr(created_at, 'tzinfo') else created_at
                m_dt = merged_at.replace(tzinfo=None) if hasattr(merged_at, 'tzinfo') else merged_at
                duration = (m_dt - c_dt).total_seconds()
                if duration >= 0:
                    merge_durations_seconds.append(duration)

    if merge_durations_seconds:
        avg_seconds = sum(merge_durations_seconds) / len(merge_durations_seconds)
        avg_hours = int(avg_seconds // 3600)
        avg_mins = int((avg_seconds % 3600) // 60)
        avg_time_to_merge_str = f"{avg_hours} hrs {avg_mins}m" if avg_hours > 0 else f"{avg_mins}m"
    else:
        avg_time_to_merge_str = "0h 0m"

    return {
        "summary": {
            "totalPRs": total,
            "openPRs": open_prs,
            "reviewed": reviewed,
            "unreviewed": unreviewed,
            "mergedPRs": merged_prs,
            "closedNoMerge": closed_no_merge,
            "avgTimeToMerge": avg_time_to_merge_str,
            "firstTimePassRate": "0%" if total == 0 else f"{int((merged_prs/total)*100)}% - {merged_prs} PRs"
        },
        "prs": prs_list
    }


@app.get("/github/pr-details/{company_name}/{pr_id:path}")
async def get_github_pr_details(company_name: str, pr_id: str):
    tenant_db = get_tenant_db(company_name)
    pr = await tenant_db.github_prs.find_one({"$or": [{"prId": pr_id}, {"prId": f"#{pr_id}"}]})
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")
    return sanitize_doc(pr)


@app.post("/github/prs/{company_name}")
async def create_or_update_pr(company_name: str, pr_data: PullRequest):
    tenant_db = get_tenant_db(company_name)
    doc = pr_data.dict(exclude_unset=True)
    doc["updatedAt"] = datetime.utcnow()

    # Convert hierarchy fields to ObjectId if valid
    if doc.get("companyId"):
        doc["companyId"] = to_object_id(doc["companyId"]) or doc["companyId"]
    if doc.get("projectId"):
        doc["projectId"] = to_object_id(doc["projectId"])
    if doc.get("boardId"):
        doc["boardId"] = to_object_id(doc["boardId"])
    if isinstance(doc.get("sprintId"), list):
        doc["sprintId"] = [to_object_id(sid) or sid for sid in doc["sprintId"]]

    await tenant_db.github_prs.update_one(
        {"prId": doc["prId"]},
        {"$set": doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
        upsert=True
    )
    return {"message": "Pull request stored in MongoDB successfully", "prId": doc["prId"]}


@app.post("/github/sync-prs/{company_name}")
async def sync_github_prs(company_name: str):
    tenant_db = get_tenant_db(company_name)

    try:
        await tenant_db.github_prs.create_index("prId", unique=True)
    except Exception as e:
        print(f"Index creation notice: {e}")

    connection = await tenant_db.connections.find_one({"integrationType": "github"})
    if not connection:
        return {"message": "GitHub connection not found", "totalSynced": 0}

    owner = connection.get("github_owner")
    token = connection.get("github_token")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    company = await tenant_db.company.find_one({"companyName": company_name})
    if not company:
        company = await meta_db.companies.find_one({"companyName": company_name})
    company_obj_id = to_object_id(company.get("_id")) if company else None

    projects = await tenant_db.projects.find().to_list(None)
    boards = await tenant_db.boards.find().to_list(None)
    sprints = await tenant_db.sprints.find().to_list(None)

    synced_count = 0
    try:
        repos = await tenant_db.github_repos.find().to_list(None)
        async with httpx.AsyncClient() as httpx_client:
            for repo in repos:
                repo_name = repo.get("name")
                if not repo_name:
                    continue
                pr_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls?state=all&per_page=50"
                res = await httpx_client.get(pr_url, headers=headers)
                if res.status_code == 200:
                    for pr in res.json():
                        pr_number = pr.get("number")
                        pr_id = f"#{pr_number}"

                        single_pr_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls/{pr_number}"
                        single_res = await httpx_client.get(single_pr_url, headers=headers)
                        single_data = single_res.json() if single_res.status_code == 200 else {}

                        files_changed = single_data.get("changed_files", 0)
                        lines_added = single_data.get("additions", 0)
                        lines_deleted = single_data.get("deletions", 0)
                        review_comments = single_data.get("review_comments", 0)

                        mergeable_val = str(single_data.get("mergeable")) if single_data.get("mergeable") is not None else "UNKNOWN"
                        is_merged = single_data.get("merged", False) or (pr.get("merged_at") is not None)
                        merged_val = "Yes" if is_merged else "No"
                        pr_merged_by = single_data.get("merged_by", {}).get("login") if single_data.get("merged_by") else None
                        branch_name = single_data.get("head", {}).get("ref") or pr.get("head", {}).get("ref")

                        files_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls/{pr_number}/files?per_page=100"
                        files_res = await httpx_client.get(files_url, headers=headers)

                        SENSITIVE_PATTERNS = [
                            r"\.env", r"credentials", r"secret", r"password", r"\.pem$", r"\.key$",
                            r"id_rsa", r"service[-_]account.*\.json$", r"config/secrets"
                        ]
                        CODE_EXTENSIONS = [".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".go", ".rb", ".php", ".c", ".cpp", ".cs", ".html", ".css", ".vue", ".svelte"]
                        TEST_PATTERNS = [r"test", r"spec", r"__tests__"]

                        sensitive_files = []
                        code_files_count = 0
                        test_files_count = 0

                        if files_res.status_code == 200:
                            for f in files_res.json():
                                fname = f.get("filename", "")
                                is_sens = any(re.search(pat, fname, re.IGNORECASE) for pat in SENSITIVE_PATTERNS)
                                if is_sens:
                                    sensitive_files.append({
                                        "filename": fname,
                                        "status": f.get("status"),
                                        "additions": f.get("additions", 0),
                                        "deletions": f.get("deletions", 0)
                                    })

                                is_test = any(re.search(pat, fname, re.IGNORECASE) for pat in TEST_PATTERNS)
                                is_code = any(fname.endswith(ext) for ext in CODE_EXTENSIONS)

                                if is_test:
                                    test_files_count += 1
                                elif is_code:
                                    code_files_count += 1

                        has_sensitive_changes = len(sensitive_files) > 0
                        has_missing_tests = (code_files_count > 0 and test_files_count == 0)
                        missing_tests = {
                            "hasMissingTests": has_missing_tests,
                            "codeFilesChanged": code_files_count,
                            "testFilesChanged": test_files_count
                        }

                        reviews_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls/{pr_number}/reviews"
                        reviews_res = await httpx_client.get(reviews_url, headers=headers)
                        reviews_data = []
                        reviewer_logins = set()
                        if reviews_res.status_code == 200:
                            for r in reviews_res.json():
                                u_login = r.get("user", {}).get("login")
                                if u_login:
                                    reviewer_logins.add(u_login)
                                sub_at = None
                                if r.get("submitted_at"):
                                    try:
                                        sub_at = datetime.fromisoformat(r["submitted_at"].replace("Z", "+00:00"))
                                    except Exception:
                                        pass
                                reviews_data.append({
                                    "id": r.get("id"),
                                    "user": u_login,
                                    "state": r.get("state"),
                                    "submittedAt": sub_at,
                                    "body": r.get("body")
                                })

                        for req in pr.get("requested_reviewers", []):
                            if req.get("login"):
                                reviewer_logins.add(req.get("login"))

                        reviewer_str = ", ".join(sorted(reviewer_logins)) if reviewer_logins else "Unassigned"

                        commits_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls/{pr_number}/commits?per_page=50"
                        commits_res = await httpx_client.get(commits_url, headers=headers)
                        commits_data = []
                        if commits_res.status_code == 200:
                            for c in commits_res.json():
                                c_date = None
                                raw_date = c.get("commit", {}).get("author", {}).get("date")
                                if raw_date:
                                    try:
                                        c_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                                    except Exception:
                                        pass
                                commits_data.append({
                                    "sha": c.get("sha"),
                                    "author": c.get("author", {}).get("login") or c.get("commit", {}).get("author", {}).get("name"),
                                    "message": c.get("commit", {}).get("message"),
                                    "date": c_date
                                })

                        title = pr.get("title", "")
                        pr_created_by = pr.get("user", {}).get("login") or "Unknown"

                        match = re.search(r"([A-Z]{2,10})-\d+", f"{title} {branch_name or ''}")
                        project_key = match.group(1) if match else "DEFAULT"

                        project_id_obj = None
                        for proj in projects:
                            if proj.get("key") == project_key or proj.get("projectKey") == project_key:
                                project_id_obj = to_object_id(proj.get("_id"))
                                break

                        board_id_obj = None
                        for brd in boards:
                            if brd.get("projectKey") == project_key or (brd.get("boardLocation") and brd["boardLocation"].get("projectKey") == project_key):
                                board_id_obj = to_object_id(brd.get("_id"))
                                break

                        sprint_ids_objs = [
                            to_object_id(s.get("_id"))
                            for s in sprints
                            if (s.get("projectKey") == project_key or str(s.get("state")).lower() == "active") and to_object_id(s.get("_id")) is not None
                        ]
                        fix_version = None

                        pr_created_at = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")) if pr.get("created_at") else datetime.utcnow()
                        pr_closed_at = datetime.fromisoformat(pr["closed_at"].replace("Z", "+00:00")) if pr.get("closed_at") else None
                        pr_merged_at = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")) if pr.get("merged_at") else None
                        days_open = (datetime.utcnow() - pr_created_at.replace(tzinfo=None)).days if pr_created_at else 0

                        pr_doc = {
                            "companyId": company_obj_id,
                            "projectId": project_id_obj,
                            "boardId": board_id_obj,
                            "fixVersion": fix_version,
                            "repo": repo_name,
                            "sprintId": sprint_ids_objs,
                            "title": title,
                            "projectKey": project_key,
                            "status": pr.get("state", "open"),
                            "prId": pr_id,
                            "prCreatedAt": pr_created_at,
                            "prClosedAt": pr_closed_at,
                            "prMergedAt": pr_merged_at,
                            "prCreatedBy": pr_created_by,
                            "prMergedBy": pr_merged_by,
                            "filesChanged": files_changed,
                            "linesAdded": lines_added,
                            "linesDeleted": lines_deleted,
                            "reviewComments": review_comments,
                            "mergeable": mergeable_val,
                            "merged": merged_val,
                            "prNumber": pr_number,
                            "branchName": branch_name,
                            "reviews": reviews_data,
                            "commits": commits_data,
                            "hasSensitiveChanges": has_sensitive_changes,
                            "sensitiveFiles": sensitive_files,
                            "missingTests": missing_tests,

                            "updatedAt": datetime.utcnow()
                        }

                        await tenant_db.github_prs.update_one(
                            {"prId": pr_doc["prId"]},
                            {"$set": pr_doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
                            upsert=True
                        )
                        synced_count += 1
    except Exception as e:
        print(f"Error syncing GitHub PRs: {e}")

    return {"message": "GitHub pull requests synced successfully", "totalSynced": synced_count}

