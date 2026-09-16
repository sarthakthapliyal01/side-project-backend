import re
from datetime import datetime
import httpx
from database import meta_db
from utils import get_tenant_db, to_object_id, parse_dt
from services.jira_client import get_jira_connection_credentials


async def sync_jira_projects(company_name: str):
    """
    Synchronizes Jira projects for the given company workspace.
    """
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
    creds = await get_jira_connection_credentials(tenant_db)

    jira_host = creds["jira_host"]
    auth = creds["auth"]
    headers = creds["headers"]

    jira_projects = []
    seen_ids = set()

    async with httpx.AsyncClient() as httpx_client:
        res1 = await httpx_client.get(f"https://{jira_host}.atlassian.net/rest/api/3/project", auth=auth, headers=headers)
        if res1.status_code == 200 and isinstance(res1.json(), list):
            for p in res1.json():
                pid = str(p.get("id"))
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    jira_projects.append(p)

        start_at = 0
        max_results = 50
        while True:
            search_url = f"https://{jira_host}.atlassian.net/rest/api/3/project/search?startAt={start_at}&maxResults={max_results}&expand=description,lead,issueTypes,url,projectKeys,permissions"
            res2 = await httpx_client.get(search_url, auth=auth, headers=headers)
            if res2.status_code != 200:
                break
            p_data = res2.json()
            values = p_data.get("values", [])
            for p in values:
                pid = str(p.get("id"))
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    jira_projects.append(p)
            if p_data.get("isLast", True) or len(values) < max_results:
                break
            start_at += len(values)

    for project in jira_projects:
        project_id = str(project.get("id"))
        set_fields = {
            "projectName": project.get("name"),
            "projectKey": project.get("key"),
            "projectType": project.get("projectTypeKey"),
            "projectStyle": project.get("style") or project.get("projectTypeKey"),
            "isSelected": True,
            "updatedAt": datetime.utcnow()
        }
        set_on_insert_fields = {
            "projectId": project_id,
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


async def sync_jira_boards(company_name: str):
    """
    Synchronizes Jira boards for the given company workspace.
    """
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

    creds = await get_jira_connection_credentials(tenant_db)
    jira_host = creds["jira_host"]
    auth = creds["auth"]
    headers = creds["headers"]

    boards = []
    seen_board_ids = set()

    async with httpx.AsyncClient() as httpx_client:
        start_at = 0
        max_results = 50
        while True:
            url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board?startAt={start_at}&maxResults={max_results}"
            response = await httpx_client.get(url, auth=auth, headers=headers)
            if response.status_code != 200:
                break
            res_data = response.json()
            values = res_data.get("values", [])
            for b in values:
                bid = b.get("id")
                if bid and bid not in seen_board_ids:
                    seen_board_ids.add(bid)
                    boards.append(b)
            if res_data.get("isLast", True) or len(values) < max_results:
                break
            start_at += len(values)

        projects = await tenant_db.projects.find().to_list(None)
        for proj in projects:
            pkey = proj.get("projectKey")
            pid = proj.get("projectId")
            for p_identifier in [pkey, pid]:
                if not p_identifier:
                    continue
                pb_url = f"https://{jira_host}.atlassian.net/rest/agile/1.0/board?projectKeyOrId={p_identifier}"
                pb_res = await httpx_client.get(pb_url, auth=auth, headers=headers)
                if pb_res.status_code == 200:
                    for b in pb_res.json().get("values", []):
                        bid = b.get("id")
                        if bid and bid not in seen_board_ids:
                            seen_board_ids.add(bid)
                            boards.append(b)

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
