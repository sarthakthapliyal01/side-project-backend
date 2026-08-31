import re
from datetime import datetime, timedelta
from typing import Optional, List, Union
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from database import meta_db
from models.board import Board
from utils import get_tenant_db, to_object_id, sanitize_doc, parse_dt
from routers.github import sync_all_github_data

router = APIRouter(tags=["Jira Integration"])


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


@router.post("/jira/test-connection")
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


@router.post("/jira/save-connection")
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


@router.get("/jira/connection/{company_name}")
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


@router.get("/jira/boards/{company_name}")
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
@router.post("/jira/sync-projects/{company_name}")
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
@router.post("/jira/sync-boards/{company_name}")
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
@router.post("/jira/sync-sprints/{company_name}")
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
@router.post("/jira/sync-sprint-issues/{company_name}/{sprint_id}")
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

    issues_data = []
    start_at = 0
    max_results = 100
    while True:
        url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{sprint_id}/issue?startAt={start_at}&maxResults={max_results}"
        async with httpx.AsyncClient() as httpx_client:
            response = await httpx_client.get(url, auth=auth, headers=headers)

        if response.status_code != 200:
            if start_at == 0 and not str(sprint_id).isdigit() and sprint_doc and sprint_doc.get("sprintId"):
                alt_sid = sprint_doc.get("sprintId")
                url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{alt_sid}/issue?startAt={start_at}&maxResults={max_results}"
                async with httpx.AsyncClient() as httpx_client:
                    response = await httpx_client.get(url, auth=auth, headers=headers)

        if response.status_code != 200:
            if start_at == 0:
                raise HTTPException(status_code=400, detail=f"Failed to fetch issues for sprint {sprint_id}")
            break

        res_json = response.json()
        batch = res_json.get("issues", [])
        issues_data.extend(batch)
        total = res_json.get("total", len(issues_data))
        start_at += len(batch)
        if start_at >= total or not batch:
            break

    synced_issues_count = 0
    synced_issue_ids = []

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
        resolution_dt = parse_dt(fields.get("resolutiondate") or fields.get("statuscategorychangedate"))
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
                "description": issue_type_data.get("description"),
                "subtask": bool(issue_type_data.get("subtask", False))
            },
            "status": {
                "id": status_data.get("id"),
                "name": status_data.get("name")
            },
            "priority": priority_data.get("name") if priority_data else None,
            "issueCreatedAt": created_dt,
            "issueUpdatedAt": updated_dt,
            "workCompletedAt": resolution_dt,
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
        synced_issue_ids.append(issue_id)

        sprint_id_list = []
        if sprint_mongo_id:
            sprint_id_list.extend([sprint_mongo_id, str(sprint_mongo_id)])
        if sprint_id is not None:
            sprint_id_list.extend([sprint_id, str(sprint_id)])
            if str(sprint_id).isdigit():
                sprint_id_list.append(int(sprint_id))

        await tenant_db.sprint_issues.update_many(
            {"sprintId": {"$in": sprint_id_list}, "issueId": {"$nin": synced_issue_ids}},
            {"$pullAll": {"sprintId": sprint_id_list}}
        )

    return {
        "message": f"Sprint issues synced successfully for sprint {sprint_id}",
        "totalSynced": synced_issues_count
    }


# 5. Modular Endpoint: Sync Capacity Members
@router.post("/jira/sync-capacity/{company_name}")
async def sync_jira_capacity(company_name: str):
    tenant_db = get_tenant_db(company_name)
    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not jira_connection:
        return {"message": "Jira connection not found", "syncedMembers": 0}

    jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
    auth = (jira_connection["jira_email"], jira_connection["jira_token"])
    headers = {"Accept": "application/json"}

    synced_members = 0

    async with httpx.AsyncClient() as httpx_client:
        projects = await tenant_db.projects.find().to_list(None)
        for proj in projects:
            pkey = proj.get("projectKey")
            if not pkey:
                continue
            try:
                user_url = f"https://{jira_host}.atlassian.net/rest/api/3/user/assignable/search?project={pkey}"
                u_res = await httpx_client.get(user_url, auth=auth, headers=headers, timeout=10.0)
                if u_res.status_code == 200 and isinstance(u_res.json(), list):
                    for u in u_res.json():
                        disp_name = u.get("displayName")
                        if disp_name and u.get("accountType") != "app":
                            member_doc = {
                                "id": u.get("accountId"),
                                "name": disp_name,
                                "email": u.get("emailAddress") or "",
                                "projectKey": pkey,
                                "updatedAt": datetime.utcnow()
                            }
                            await tenant_db.capacity_members.update_one(
                                {"name": disp_name, "projectKey": pkey},
                                {"$set": member_doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
                                upsert=True
                            )
                            synced_members += 1
            except Exception as e:
                print(f"Error syncing capacity users for project {pkey}: {e}")

    return {"message": "Capacity members synced successfully", "syncedMembers": synced_members}


# ORCHESTRATED UNIFIED SYNC: Calls sync_jira_projects, sync_jira_boards, sync_jira_sprints, sync_jira_sprint_issues, & sync_jira_capacity sequentially
@router.post("/jira/sync-all/{company_name}")
@router.post("/sync-all/{company_name}")
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
    cap_res = {}
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

            # 5. Sync Capacity / Team Members
            cap_res = await sync_jira_capacity(company_name)
        except Exception as e:
            print(f"Error syncing Jira data: {e}")

    if github_conn:
        # 6. Sync GitHub Repos & PRs if GitHub integration exists
        try:
            await sync_all_github_data(company_name)
        except Exception as e:
            print(f"Error syncing GitHub data in sync-all: {e}")

    return {
        "message": "Data synced successfully",
        "totalProjects": proj_res.get("totalProjects", 0),
        "totalBoards": boards_res.get("totalBoards", 0),
        "totalSprints": sprints_res.get("totalSynced", 0),
        "totalSprintIssues": total_issues,
        "totalCapacityMembers": cap_res.get("syncedMembers", 0)
    }


@router.get("/jira/projects/{company_name}")
async def get_jira_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)
    projects = await tenant_db.projects.find().to_list(None)
    projects_list = [sanitize_doc(project) for project in projects]
    return {"totalProjects": len(projects_list), "projects": projects_list}


@router.put("/jira/project-selection/{company_name}")
async def update_project_selection(company_name: str, data: ProjectSelectionRequest):
    tenant_db = get_tenant_db(company_name)
    result = await tenant_db.projects.update_one(
        {"projectId": data.projectId},
        {"$set": {"isSelected": data.isSelected, "updatedAt": datetime.utcnow()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"message": "Project selection updated"}


@router.get("/jira/selected-projects/{company_name}")
async def get_selected_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)
    projects = await tenant_db.projects.find({"isSelected": True}).to_list(length=None)
    projects_list = [sanitize_doc(project) for project in projects]
    return {"projects": projects_list}


async def find_target_sprint_doc(tenant_db, sprint_id: str, project_id: Optional[str] = None):
    if not sprint_id:
        return None

    s_id_str = str(sprint_id).strip()
    s_conds = [{"name": sprint_id}]
    if s_id_str.isdigit():
        s_conds.extend([{"sprintId": int(s_id_str)}, {"sprintId": s_id_str}])
    else:
        s_conds.append({"sprintId": s_id_str})

    proj_doc = None
    if project_id:
        or_conds = [
            {"projectId": str(project_id)},
            {"projectKey": str(project_id)},
            {"projectName": str(project_id)}
        ]
        if str(project_id).isdigit():
            or_conds.append({"projectId": int(project_id)})
        obj_p = to_object_id(project_id)
        if obj_p:
            or_conds.append({"_id": obj_p})
        proj_doc = await tenant_db.projects.find_one({"$or": or_conds})

    if proj_doc:
        p_key = proj_doc.get("projectKey")
        p_obj_id = proj_doc.get("_id")
        p_id = proj_doc.get("projectId")

        p_filters = []
        if p_key:
            p_filters.append({"projectKey": p_key})
        if p_obj_id:
            p_filters.append({"projectId": p_obj_id})
        if p_id:
            p_filters.append({"projectId": p_id})

        if p_filters:
            query = {"$and": [{"$or": s_conds}, {"$or": p_filters}]}
            doc = await tenant_db.sprints.find_one(query)
            if doc:
                return doc

    return await tenant_db.sprints.find_one({"$or": s_conds})


@router.get("/jira/capacity-data/{company_name}")
async def get_jira_capacity_data(company_name: str, sprint_id: Optional[str] = None, project_id: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)
    jira_connection = await tenant_db.connections.find_one({"integrationType": "jira"})

    members_dict = {}

    # Check existing assignees saved in target sprint document
    saved_assignees_map = {}
    if sprint_id:
        target_sprint_doc = await find_target_sprint_doc(tenant_db, sprint_id, project_id)
        if target_sprint_doc and "assignees" in target_sprint_doc and isinstance(target_sprint_doc["assignees"], list):
            for ass in target_sprint_doc["assignees"]:
                name_key = str(ass.get("assignee") or "").strip().lower()
                if name_key:
                    saved_assignees_map[name_key] = ass

    if jira_connection and project_id:
        jira_host = jira_connection["jira_host"].replace(".atlassian.net", "").replace("https://", "")
        auth = (jira_connection["jira_email"], jira_connection["jira_token"])
        headers = {"Accept": "application/json"}

        # Resolve Jira Project Key and ID
        jira_project_key = None
        jira_project_id = None

        or_conds = [
            {"projectId": str(project_id)},
            {"projectKey": str(project_id)},
            {"projectName": str(project_id)}
        ]
        if str(project_id).isdigit():
            or_conds.append({"projectId": int(project_id)})
        obj_p = to_object_id(project_id)
        if obj_p:
            or_conds.append({"_id": obj_p})

        proj_doc = await tenant_db.projects.find_one({"$or": or_conds})
        if proj_doc:
            jira_project_key = proj_doc.get("projectKey")
            jira_project_id = str(proj_doc.get("projectId") or "")
        else:
            jira_project_key = str(project_id)
            jira_project_id = str(project_id)

        candidate_pids = []
        if jira_project_key:
            candidate_pids.append(jira_project_key)
        if jira_project_id and jira_project_id not in candidate_pids:
            candidate_pids.append(jira_project_id)

        async with httpx.AsyncClient() as httpx_client:
            # 1. Fetch explicit Project Role actors from Jira API
            explicit_user_names = set()
            for pid in candidate_pids:
                if not pid:
                    continue
                try:
                    roles_url = f"https://{jira_host}.atlassian.net/rest/api/3/project/{pid}/role"
                    r_res = await httpx_client.get(roles_url, auth=auth, headers=headers, timeout=10.0)
                    if r_res.status_code == 200 and isinstance(r_res.json(), dict):
                        role_dict = r_res.json()
                        for r_name, r_url in role_dict.items():
                            try:
                                actor_res = await httpx_client.get(r_url, auth=auth, headers=headers, timeout=10.0)
                                if actor_res.status_code == 200 and isinstance(actor_res.json(), dict):
                                    actors = actor_res.json().get("actors", [])
                                    for actor in actors:
                                        actor_user = actor.get("actorUser") or {}
                                        disp = actor.get("displayName") or actor_user.get("displayName")
                                        if disp:
                                            explicit_user_names.add(disp.strip().lower())
                            except Exception as e:
                                print(f"Error fetching role details for {r_url}: {e}")
                except Exception as e:
                    print(f"Error fetching project roles for {pid}: {e}")

            # 2. Fetch assignees with issues in this project/sprint from Jira or DB
            sprint_issue_assignees = {}
            if sprint_id:
                try:
                    iss_url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/sprint/{sprint_id}/issue"
                    i_res = await httpx_client.get(iss_url, auth=auth, headers=headers, timeout=10.0)
                    if i_res.status_code == 200:
                        issues_data = i_res.json().get("issues", [])
                        for issue in issues_data:
                            fields = issue.get("fields", {})
                            assignee_data = fields.get("assignee")
                            if not assignee_data:
                                continue

                            disp_name = assignee_data.get("displayName")
                            if not disp_name:
                                continue

                            sp = 0.0
                            for cf_key in ["customfield_10016", "customfield_10026", "customfield_10002", "customfield_10004", "storyPoints"]:
                                if fields.get(cf_key) is not None:
                                    try:
                                        sp = float(fields.get(cf_key))
                                        break
                                    except (ValueError, TypeError):
                                        pass

                            if sp == 0.0:
                                orig_sec = fields.get("timeoriginalestimate") or 0
                                if orig_sec:
                                    sp = round(float(orig_sec) / 3600.0, 1)

                            disp_lower = disp_name.strip().lower()
                            if disp_lower not in sprint_issue_assignees:
                                sprint_issue_assignees[disp_lower] = {
                                    "id": assignee_data.get("accountId"),
                                    "name": disp_name,
                                    "email": "",
                                    "sp": sp
                                }
                            else:
                                sprint_issue_assignees[disp_lower]["sp"] += sp
                except Exception as e:
                    print(f"Error fetching sprint issues: {e}")

            # 3. Fetch assignable users from Jira API for this project
            assignable_jira_users = []
            for pid in candidate_pids:
                if not pid:
                    continue
                try:
                    user_url = f"https://{jira_host}.atlassian.net/rest/api/3/user/assignable/search?project={pid}"
                    u_res = await httpx_client.get(user_url, auth=auth, headers=headers, timeout=10.0)
                    if u_res.status_code == 200 and isinstance(u_res.json(), list):
                        users_list = u_res.json()
                        if users_list:
                            assignable_jira_users = users_list
                            break
                except Exception as e:
                    print(f"Error fetching assignable users: {e}")

            # 4. Filter assignable users:
            for u in assignable_jira_users:
                disp_name = u.get("displayName")
                if not disp_name or u.get("accountType") == "app":
                    continue

                disp_lower = disp_name.strip().lower()
                has_sprint_issues = disp_lower in sprint_issue_assignees
                is_in_project_roles = disp_lower in explicit_user_names

                # Filter out users who are NOT in project roles AND have NO sprint issues in this project
                if explicit_user_names and not is_in_project_roles and not has_sprint_issues:
                    continue

                sp = sprint_issue_assignees.get(disp_lower, {}).get("sp", 0.0)
                saved_info = saved_assignees_map.get(disp_lower, {})

                role = saved_info.get("role") or "Developer"
                billing_rate = float(saved_info.get("billingRate") if saved_info.get("billingRate") is not None else 0.0)
                avail_cap = float(saved_info.get("availableCapacity") if saved_info.get("availableCapacity") is not None else (saved_info.get("availableHours") if saved_info.get("availableHours") is not None else 0.0))
                leaves_val = float(saved_info.get("leaves") if saved_info.get("leaves") is not None else (saved_info.get("leave") if saved_info.get("leave") is not None else 0.0))
                net_avail = float(saved_info.get("netAvailableCapacity") if saved_info.get("netAvailableCapacity") is not None else (avail_cap - leaves_val))
                email = saved_info.get("email") or ""
                alloc_type = saved_info.get("allocationType") or "Full"
                alloc_cap = sp
                rem_cap = net_avail - alloc_cap

                members_dict[disp_name] = {
                    "id": u.get("accountId") or str(len(members_dict) + 1),
                    "name": disp_name,
                    "email": email,
                    "role": role,
                    "allocationType": alloc_type,
                    "availableCapacity": avail_cap,
                    "leaves": leaves_val,
                    "netAvailableCapacity": net_avail,
                    "allocatedCapacity": alloc_cap,
                    "remainingCapacity": rem_cap,
                    "billingRate": billing_rate,
                    "totalBillingRate": billing_rate * alloc_cap,
                    "fromJira": True,
                }

            # Add any sprint issue assignees who might not have been returned in assignable search
            for disp_lower, assignee in sprint_issue_assignees.items():
                disp_name = assignee["name"]
                if disp_name not in members_dict:
                    sp = assignee["sp"]
                    saved_info = saved_assignees_map.get(disp_lower, {})
                    role = saved_info.get("role") or "Developer"
                    billing_rate = float(saved_info.get("billingRate") if saved_info.get("billingRate") is not None else 0.0)
                    avail_cap = float(saved_info.get("availableCapacity") if saved_info.get("availableCapacity") is not None else (saved_info.get("availableHours") if saved_info.get("availableHours") is not None else 0.0))
                    leaves_val = float(saved_info.get("leaves") if saved_info.get("leaves") is not None else (saved_info.get("leave") if saved_info.get("leave") is not None else 0.0))
                    net_avail = float(saved_info.get("netAvailableCapacity") if saved_info.get("netAvailableCapacity") is not None else (avail_cap - leaves_val))
                    email = saved_info.get("email") or ""
                    alloc_type = saved_info.get("allocationType") or "Full"

                    members_dict[disp_name] = {
                        "id": assignee["id"] or str(len(members_dict) + 1),
                        "name": disp_name,
                        "email": email,
                        "role": role,
                        "allocationType": alloc_type,
                        "availableCapacity": avail_cap,
                        "leaves": leaves_val,
                        "netAvailableCapacity": net_avail,
                        "allocatedCapacity": sp,
                        "remainingCapacity": net_avail - sp,
                        "billingRate": billing_rate,
                        "totalBillingRate": billing_rate * sp,
                        "fromJira": True,
                    }

    # Add any saved members from MongoDB assignees that were not in Jira's user list (e.g. manually added users)
    for name_key, saved_info in saved_assignees_map.items():
        disp_name = saved_info.get("assignee") or saved_info.get("name")
        if disp_name and disp_name not in members_dict:
            avail_cap = float(saved_info.get("availableCapacity") if saved_info.get("availableCapacity") is not None else (saved_info.get("availableHours") if saved_info.get("availableHours") is not None else 0.0))
            leaves_val = float(saved_info.get("leaves") if saved_info.get("leaves") is not None else (saved_info.get("leave") if saved_info.get("leave") is not None else 0.0))
            net_avail = float(saved_info.get("netAvailableCapacity") if saved_info.get("netAvailableCapacity") is not None else (avail_cap - leaves_val))
            alloc_cap = float(saved_info.get("allocatedCapacity") if saved_info.get("allocatedCapacity") is not None else (saved_info.get("allocatedHours") if saved_info.get("allocatedHours") is not None else 0.0))
            billing_rate = float(saved_info.get("billingRate") if saved_info.get("billingRate") is not None else 0.0)
            from_jira = saved_info.get("fromJira") if saved_info.get("fromJira") is not None else (saved_info.get("addedManually") != "yes")

            members_dict[disp_name] = {
                "id": saved_info.get("id") or str(len(members_dict) + 1),
                "name": disp_name,
                "email": saved_info.get("email") or "",
                "role": saved_info.get("role") or "Developer",
                "allocationType": saved_info.get("allocationType") or "Full",
                "availableCapacity": avail_cap,
                "leaves": leaves_val,
                "netAvailableCapacity": net_avail,
                "allocatedCapacity": alloc_cap,
                "remainingCapacity": net_avail - alloc_cap,
                "billingRate": billing_rate,
                "totalBillingRate": billing_rate * alloc_cap,
                "fromJira": from_jira,
            }

    members_list = list(members_dict.values())
    return {"members": members_list}


class CapacitySaveMember(BaseModel):
    id: Optional[Union[str, int]] = None
    name: str
    email: Optional[str] = ""
    role: Optional[str] = "Developer"
    allocationType: Optional[str] = "Full"
    availableCapacity: Optional[float] = 0.0
    leaves: Optional[float] = 0.0
    netAvailableCapacity: Optional[float] = 0.0
    allocatedCapacity: Optional[float] = 0.0
    remainingCapacity: Optional[float] = 0.0
    billingRate: Optional[float] = 0.0
    totalBillingRate: Optional[float] = 0.0
    fromJira: Optional[bool] = True
    tickets: Optional[int] = 0

class CapacitySaveRequest(BaseModel):
    sprint_id: Optional[str] = None
    project_id: Optional[str] = None
    storyPointHrs: Optional[float] = 8.0
    members: List[CapacitySaveMember]

@router.post("/jira/save-capacity/{company_name}")
async def save_jira_capacity(company_name: str, payload: CapacitySaveRequest):
    tenant_db = get_tenant_db(company_name)

    assignees_array = []
    for m in payload.members:
        m_dict = m.dict()
        avail_hrs = float(m_dict.get("availableCapacity") if m_dict.get("availableCapacity") is not None else 0.0)
        leaves_val = float(m_dict.get("leaves") if m_dict.get("leaves") is not None else (m_dict.get("leave") if m_dict.get("leave") is not None else 0.0))
        net_avail = avail_hrs - leaves_val
        alloc_hrs = float(m_dict.get("allocatedCapacity") if m_dict.get("allocatedCapacity") is not None else 0.0)
        rem_cap = net_avail - alloc_hrs
        rate = float(m_dict.get("billingRate") or 0.0)
        from_jira = m_dict.get("fromJira", True)
        assignee_item = {
            "id": m_dict.get("id"),
            "assignee": m_dict.get("name", ""),
            "name": m_dict.get("name", ""),
            "email": m_dict.get("email", "") or "",
            "availableHours": avail_hrs,
            "availableCapacity": avail_hrs,
            "leaves": leaves_val,
            "leave": leaves_val,
            "netAvailableCapacity": net_avail,
            "allocationType": m_dict.get("allocationType") or "Full",
            "allocatedHours": alloc_hrs,
            "allocatedCapacity": alloc_hrs,
            "remainingCapacity": rem_cap,
            "initialPlannedAllocation": 0,
            "tickets": int(m_dict.get("tickets") or 0),
            "role": m_dict.get("role") or "Developer",
            "billingRate": rate,
            "totalBillingRate": rate * alloc_hrs,
            "holiday": 0,
            "addedManually": "no" if from_jira else "yes",
            "fromJira": from_jira,
            "sprintOrReleaseUser": "yes",
            "presentInPlan": "yes",
            "planLockedLocally": "no",
            "addUserModalLocked": "no",
            "modified": False
        }
        assignees_array.append(assignee_item)

    if payload.sprint_id:
        target_sprint_doc = await find_target_sprint_doc(tenant_db, payload.sprint_id, payload.project_id)
        if target_sprint_doc:
            await tenant_db.sprints.update_one(
                {"_id": target_sprint_doc["_id"]},
                {"$set": {
                    "assignees": assignees_array,
                    "assigneeCopiedForToday": True,
                    "updatedAt": datetime.utcnow()
                }}
            )
        else:
            s_id_str = str(payload.sprint_id).strip()
            s_id_val = int(s_id_str) if s_id_str.isdigit() else payload.sprint_id
            await tenant_db.sprints.update_one(
                {"$or": [{"sprintId": s_id_val}, {"sprintId": s_id_str}, {"name": payload.sprint_id}]},
                {"$set": {
                    "sprintId": s_id_val,
                    "projectId": payload.project_id,
                    "assignees": assignees_array,
                    "assigneeCopiedForToday": True,
                    "updatedAt": datetime.utcnow()
                },
                "$setOnInsert": {
                    "name": str(payload.sprint_id),
                    "createdAt": datetime.utcnow()
                }},
                upsert=True
            )

    return {"message": "Capacity saved successfully", "assignees": assignees_array}


@router.get("/jira/sprints/{company_name}")
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


@router.get("/jira/db-sprints/{company_name}")
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
    def get_sprint_num(s):
        name = str(s.get("name") or "")
        matches = re.findall(r'\d+', name)
        if matches:
            return int(matches[-1])
        sid = s.get("sprintId") or s.get("_id")
        if str(sid).isdigit():
            return int(sid)
        return 999999
    sprints = sorted(sprints, key=get_sprint_num)
    sprints_list = [sanitize_doc(s) for s in sprints]
    return {"sprints": sprints_list}


@router.get("/jira/db-sprint-issues/{company_name}")
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


@router.get("/jira/burndown/{company_name}")
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
            
            comp_dt = parse_dt(issue.get("workCompletedAt") or issue.get("resolutiondate") or issue.get("issueUpdatedAt") or issue.get("issueCreatedAt"))
            c_date = comp_dt.date() if comp_dt else start_dt.date()
            # Shift weekend completions (Saturday/Sunday) to Monday so actual lines freeze flat over weekends
            if c_date.weekday() == 5:
                c_date = c_date + timedelta(days=2)
            elif c_date.weekday() == 6:
                c_date = c_date + timedelta(days=1)

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


@router.get("/jira/burnup/{company_name}")
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
            
            comp_dt = parse_dt(issue.get("workCompletedAt") or issue.get("resolutiondate") or issue.get("issueUpdatedAt") or issue.get("issueCreatedAt"))
            c_date = comp_dt.date() if comp_dt else start_dt.date()
            # Shift weekend completions (Saturday/Sunday) to Monday so actual lines freeze flat over weekends
            if c_date.weekday() == 5:
                c_date = c_date + timedelta(days=2)
            elif c_date.weekday() == 6:
                c_date = c_date + timedelta(days=1)

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


class RoleBillingItem(BaseModel):
    role: str
    billingRate: float

class RolesBillingRequest(BaseModel):
    roles: List[RoleBillingItem]

@router.get("/jira/roles-billing/{company_name}")
async def get_roles_billing(company_name: str):
    tenant_db = get_tenant_db(company_name)
    doc = await tenant_db.roles_billing.find_one({"type": "roles_config"})
    if doc and "roles" in doc:
        return {"roles": doc["roles"]}
    
    default_roles = [
        {"role": "Developer", "billingRate": 30.0},
        {"role": "Manager", "billingRate": 30.0},
        {"role": "Tester", "billingRate": 20.0},
        {"role": "DB team", "billingRate": 10.0},
    ]
    return {"roles": default_roles}

@router.post("/jira/roles-billing/{company_name}")
async def save_roles_billing(company_name: str, payload: RolesBillingRequest):
    tenant_db = get_tenant_db(company_name)
    roles_list = [r.dict() for r in payload.roles]
    await tenant_db.roles_billing.update_one(
        {"type": "roles_config"},
        {"$set": {"roles": roles_list, "updatedAt": datetime.utcnow()}},
        upsert=True
    )
    return {"message": "Roles and billing rates saved successfully", "roles": roles_list}


@router.get("/jira/sprint-goal-success/{company_name}")
async def get_jira_sprint_goal_success(company_name: str, project_id: Optional[str] = None):
    tenant_db = get_tenant_db(company_name)

    query = {}
    if project_id:
        possible_ids = [project_id]
        if str(project_id).isdigit():
            possible_ids.append(int(project_id))
        obj_id = to_object_id(project_id)
        if obj_id:
            possible_ids.append(obj_id)

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

    sprints_cursor = tenant_db.sprints.find(query).sort("createdAt", -1).limit(6)
    recent_sprints = await sprints_cursor.to_list(None)
    recent_sprints.reverse()

    result_sprints = []

    for s_doc in recent_sprints:
        sid = s_doc.get("sprintId")
        possible_sprint_ids = []
        if sid:
            possible_sprint_ids.extend([sid, str(sid)])
            if str(sid).isdigit():
                possible_sprint_ids.append(int(sid))
        if "_id" in s_doc:
            possible_sprint_ids.extend([s_doc["_id"], str(s_doc["_id"])])

        issues = []
        if possible_sprint_ids:
            issues = await tenant_db.sprint_issues.find({"sprintId": {"$in": possible_sprint_ids}}).to_list(None)

        committed_sp_without_added = 0.0
        committed_sp_with_added = 0.0
        completed_sp = 0.0

        committed_hrs_without_added = 0.0
        committed_hrs_with_added = 0.0
        completed_hrs = 0.0

        for issue in issues:
            sp = float(issue.get("storyPoints") or 0.0)
            hrs = float(issue.get("originalEstimateHrs") or issue.get("timeSpentHrs") or (sp * 8.0))

            is_added = bool(issue.get("addedDuringSprint") or issue.get("added_during_sprint") or issue.get("isAdded"))

            committed_sp_with_added += sp
            committed_hrs_with_added += hrs

            if not is_added:
                committed_sp_without_added += sp
                committed_hrs_without_added += hrs

            st = issue.get("status")
            st_name = (st.get("name") if isinstance(st, dict) else str(st or "")).lower()
            if any(kw in st_name for kw in ["close", "done", "resolve", "complete"]):
                completed_sp += sp
                completed_hrs += hrs

        if committed_sp_without_added == 0 and committed_sp_with_added > 0:
            committed_sp_without_added = committed_sp_with_added
            committed_hrs_without_added = committed_hrs_with_added

        goal_met = bool(completed_sp >= committed_sp_with_added) if committed_sp_with_added > 0 else False

        result_sprints.append({
            "sprintId": str(s_doc.get("sprintId") or s_doc.get("_id")),
            "sprintName": s_doc.get("name", "Sprint"),
            "sp": {
                "committed": round(committed_sp_without_added, 1),
                "committedWithAdded": round(committed_sp_with_added, 1),
                "completed": round(completed_sp, 1),
            },
            "hrs": {
                "committed": round(committed_hrs_without_added, 1),
                "committedWithAdded": round(committed_hrs_with_added, 1),
                "completed": round(completed_hrs, 1),
            },
            "goalMet": goal_met
        })

    return {"sprints": result_sprints}


@router.get("/jira/churn-data/{company_name}")
async def get_jira_churn_data(
    company_name: str,
    project_id: Optional[str] = None,
    include_bugs: Optional[Union[bool, str]] = True
):
    tenant_db = get_tenant_db(company_name)

    query = {}
    if project_id:
        possible_ids = [project_id]
        if str(project_id).isdigit():
            possible_ids.append(int(project_id))
        obj_id = to_object_id(project_id)
        if obj_id:
            possible_ids.append(obj_id)

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

    db_sprints = await tenant_db.sprints.find(query).to_list(None)
    def get_sprint_num(s):
        name = str(s.get("name") or "")
        matches = re.findall(r'\d+', name)
        if matches:
            return int(matches[-1])
        sid = s.get("sprintId") or s.get("_id")
        if str(sid).isdigit():
            return int(sid)
        return 999999
    db_sprints = sorted(db_sprints, key=get_sprint_num)

    sprint_churn_list = []

    # Process bug filter options
    bug_filter_mode = "all"
    if isinstance(include_bugs, bool):
        bug_filter_mode = "all" if include_bugs else "exclude_bugs"
    elif isinstance(include_bugs, str):
        low_val = include_bugs.lower().strip()
        if low_val in ["false", "exclude", "exclude_bugs"]:
            bug_filter_mode = "exclude_bugs"
        elif low_val in ["only", "only_bugs"]:
            bug_filter_mode = "only_bugs"
        else:
            bug_filter_mode = "all"

    for s_doc in db_sprints:
        sprint_name = s_doc.get("name") or f"Sprint {s_doc.get('sprintId') or s_doc.get('_id')}"
        sid = s_doc.get("sprintId") or s_doc.get("_id")

        possible_sprint_ids = []
        if sid:
            possible_sprint_ids.extend([sid, str(sid)])
            if str(sid).isdigit():
                possible_sprint_ids.append(int(sid))
        if "_id" in s_doc:
            possible_sprint_ids.extend([s_doc["_id"], str(s_doc["_id"])])

        issues = []
        if possible_sprint_ids:
            issues = await tenant_db.sprint_issues.find({"sprintId": {"$in": possible_sprint_ids}}).to_list(None)

        start_dt = s_doc.get("startDate")
        if isinstance(start_dt, str):
            start_dt = parse_dt(start_dt)

        end_dt = s_doc.get("endDate")
        if isinstance(end_dt, str):
            end_dt = parse_dt(end_dt)

        def compute_metrics(filtered_issues):
            planned = 0
            added = 0
            removed = 0
            for issue in filtered_issues:
                is_added = bool(issue.get("addedDuringSprint") or issue.get("added_during_sprint") or issue.get("isAdded"))
                is_removed = bool(issue.get("removedDuringSprint") or issue.get("isRemoved") or issue.get("isDeleted"))

                issue_created = issue.get("issueCreatedAt") or issue.get("createdAt")
                if isinstance(issue_created, str):
                    issue_created = parse_dt(issue_created)

                if not is_added and start_dt and issue_created:
                    if issue_created > start_dt:
                        is_added = True

                if is_removed:
                    removed += 1
                elif is_added:
                    added += 1
                else:
                    planned += 1

            if planned == 0:
                if (added + removed) > 0:
                    calc = float(added + removed) * 100.0
                    churn_val = round(calc, 1)
                    churn_str = f"{churn_val}"
                else:
                    churn_val = 0.0
                    churn_str = "0.0"
            else:
                calc = ((added + removed) / float(planned)) * 100.0
                churn_val = round(calc, 1)
                churn_str = f"{churn_val}"

            return {
                "planned": planned,
                "added": added,
                "removed": removed,
                "churn": churn_str,
                "churnVal": churn_val
            }

        def extract_issue_type_name(issue: dict) -> str:
            itype = issue.get("type") or issue.get("issueType") or issue.get("issuetype") or issue.get("issue_type")
            if not itype and isinstance(issue.get("fields"), dict):
                itype = issue["fields"].get("issuetype") or issue["fields"].get("issueType")
            if isinstance(itype, dict):
                return str(itype.get("name") or "").strip()
            elif isinstance(itype, str):
                return itype.strip()
            return ""

        def is_subtask_issue(issue: dict) -> bool:
            itype = issue.get("type") or issue.get("issueType") or issue.get("issuetype") or issue.get("issue_type")
            if not itype and isinstance(issue.get("fields"), dict):
                itype = issue["fields"].get("issuetype") or issue["fields"].get("issueType")
            if isinstance(itype, dict):
                if itype.get("subtask") is True:
                    return True
                name = str(itype.get("name") or "").lower()
                if "sub-task" in name or "subtask" in name:
                    return True
            elif isinstance(itype, str):
                low = itype.lower()
                if "sub-task" in low or "subtask" in low:
                    return True
            return False

        def matches_issue_category(itype_name: str, category: str) -> bool:
            low = itype_name.lower()
            if category == "Bug":
                return any(k in low for k in ["bug", "defect", "problem", "incident"])
            elif category == "Story":
                return any(k in low for k in ["story", "feature", "requirement", "epic"])
            elif category == "Task":
                return any(k in low for k in ["task", "improvement", "job"]) and not ("sub" in low)
            return False

        valid_issues = []
        for issue in issues:
            if is_subtask_issue(issue):
                continue

            itype_name = extract_issue_type_name(issue)
            is_bug = matches_issue_category(itype_name, "Bug")

            if bug_filter_mode == "exclude_bugs" and is_bug:
                continue
            if bug_filter_mode == "only_bugs" and not is_bug:
                continue
            valid_issues.append(issue)

        by_type = {}
        for it_name in ["All", "Story", "Task", "Bug"]:
            if it_name == "All":
                by_type["All"] = compute_metrics(valid_issues)
            else:
                matching = []
                for issue in valid_issues:
                    itype_name = extract_issue_type_name(issue)
                    if matches_issue_category(itype_name, it_name):
                        matching.append(issue)
                by_type[it_name] = compute_metrics(matching)

        sprint_churn_list.append({
            "sprintId": str(sid),
            "sprint": sprint_name,
            "startDate": s_doc.get("startDate"),
            "endDate": s_doc.get("endDate"),
            "metrics": by_type
        })

    # Summary calculation (latest sprint churn)
    summary_churn = "N/A"
    if sprint_churn_list:
        latest_all = sprint_churn_list[0].get("metrics", {}).get("All", {})
        if latest_all.get("churn") and latest_all.get("churn") != "N/A":
            summary_churn = f"{latest_all.get('churn')}"

    return {
        "churnData": sprint_churn_list,
        "summaryChurn": summary_churn
    }
