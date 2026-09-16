import re
import base64
from datetime import datetime
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import meta_db
from utils import get_tenant_db, sanitize_doc

router = APIRouter(tags=["Azure Boards Integration"])


class AzureConnectionRequest(BaseModel):
    organization: str
    pat: str
    azure_url: Optional[str] = "https://dev.azure.com"


class SaveAzureRequest(BaseModel):
    companyName: str
    organization: str
    pat: str
    azure_url: Optional[str] = "https://dev.azure.com"


class ProjectSelectionRequest(BaseModel):
    projectId: str
    isSelected: bool


def clean_org_url(org: str, custom_url: Optional[str] = None) -> tuple[str, str]:
    """Returns (organization_name, api_base_url)"""
    org_name = org.strip().rstrip("/")
    if org_name.startswith("http://") or org_name.startswith("https://"):
        parts = org_name.split("/")
        org_name = parts[-1] if parts[-1] else parts[-2]

    base_host = (custom_url or "https://dev.azure.com").strip().rstrip("/")
    if not base_host.startswith("http"):
        base_host = f"https://{base_host}"

    base_url = f"{base_host}/{org_name}"
    return org_name, base_url


def get_auth_headers(pat: str) -> dict:
    """Azure DevOps PAT authentication header (Basic base64(':' + PAT))"""
    pat_clean = pat.strip()
    encoded_pat = base64.b64encode(f":{pat_clean}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {encoded_pat}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


@router.post("/azure-boards/test-connection")
async def test_azure_connection(data: AzureConnectionRequest):
    org_name, base_url = clean_org_url(data.organization, data.azure_url)
    pat = data.pat.strip()

    if not org_name:
        raise HTTPException(status_code=400, detail="Organization name is required")
    if not pat:
        raise HTTPException(status_code=400, detail="Personal Access Token (PAT) is required")

    url = f"{base_url}/_apis/projects?api-version=7.0"
    headers = get_auth_headers(pat)

    async with httpx.AsyncClient(timeout=15.0) as httpx_client:
        try:
            response = await httpx_client.get(url, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to reach Azure DevOps: {str(e)}")

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Personal Access Token (PAT) or unauthorized access.")
    elif response.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Azure DevOps organization '{org_name}' not found.")
    elif response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Azure DevOps API error ({response.status_code}): {response.text[:200]}"
        )

    res_json = response.json()
    projects = res_json.get("value", [])
    return {
        "connected": True,
        "organization": org_name,
        "project_count": len(projects),
        "projects": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description", ""),
                "state": p.get("state", "")
            }
            for p in projects
        ]
    }


@router.post("/azure-boards/save-connection")
async def save_azure_connection(data: SaveAzureRequest):
    org_name, base_url = clean_org_url(data.organization, data.azure_url)
    pat = data.pat.strip()

    if not data.companyName:
        raise HTTPException(status_code=400, detail="Company name is required")

    tenant_db = get_tenant_db(data.companyName)
    connection_data = {
        "integrationType": "azure-boards",
        "organization": org_name,
        "azure_url": base_url,
        "pat": pat,
        "status": "connected",
        "updatedAt": datetime.utcnow()
    }

    await tenant_db.connections.update_one(
        {"integrationType": "azure-boards"},
        {"$set": connection_data},
        upsert=True
    )
    return {"message": "Azure Boards connection saved successfully"}


@router.get("/azure-boards/connection/{company_name}")
async def get_azure_connection(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "azure-boards"})
    if not connection:
        return {"connected": False}
    return {
        "connected": True,
        "organization": connection.get("organization"),
        "azure_url": connection.get("azure_url", "https://dev.azure.com")
    }


@router.post("/azure-boards/sync-projects/{company_name}")
async def sync_azure_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "azure-boards"})
    if not connection:
        raise HTTPException(status_code=400, detail="Azure Boards integration not configured.")

    org_name = connection.get("organization")
    azure_url = connection.get("azure_url", f"https://dev.azure.com/{org_name}")
    pat = connection.get("pat")

    url = f"{azure_url}/_apis/projects?api-version=7.0"
    headers = get_auth_headers(pat)

    async with httpx.AsyncClient(timeout=15.0) as httpx_client:
        response = await httpx_client.get(url, headers=headers)

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch projects from Azure DevOps.")

    data = response.json()
    azure_projects = data.get("value", [])
    synced_projects = []

    for p in azure_projects:
        project_id = str(p.get("id"))
        project_name = p.get("name")
        set_fields = {
            "projectName": project_name,
            "projectKey": project_name,
            "description": p.get("description", ""),
            "state": p.get("state", ""),
            "updatedAt": datetime.utcnow()
        }
        set_on_insert_fields = {
            "projectId": project_id,
            "source": "azure-boards",
            "isSelected": False,
            "isIntegrated": True,
            "createdAt": datetime.utcnow()
        }

        await tenant_db.azure_projects.update_one(
            {"projectId": project_id},
            {"$set": set_fields, "$setOnInsert": set_on_insert_fields},
            upsert=True
        )

        doc = await tenant_db.azure_projects.find_one({"projectId": project_id})
        if doc:
            synced_projects.append(sanitize_doc(doc))

    return {
        "message": f"Synced {len(synced_projects)} Azure Boards projects",
        "projects": synced_projects
    }


@router.get("/azure-boards/projects/{company_name}")
async def get_azure_projects(company_name: str):
    tenant_db = get_tenant_db(company_name)
    projects = await tenant_db.azure_projects.find().to_list(None)
    return {"projects": [sanitize_doc(p) for p in projects]}


@router.put("/azure-boards/project-selection/{company_name}")
async def update_azure_project_selection(company_name: str, data: ProjectSelectionRequest):
    tenant_db = get_tenant_db(company_name)
    res = await tenant_db.azure_projects.update_one(
        {"projectId": data.projectId},
        {"$set": {"isSelected": data.isSelected, "updatedAt": datetime.utcnow()}}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"message": "Project selection updated successfully"}


@router.post("/azure-boards/sync-work-items/{company_name}")
async def sync_azure_work_items(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "azure-boards"})
    if not connection:
        raise HTTPException(status_code=400, detail="Azure Boards integration not configured.")

    org_name = connection.get("organization")
    azure_url = connection.get("azure_url", f"https://dev.azure.com/{org_name}")
    pat = connection.get("pat")
    headers = get_auth_headers(pat)

    # Get selected projects
    selected_projects = await tenant_db.azure_projects.find({"isSelected": True}).to_list(None)
    if not selected_projects:
        # Fallback to all projects if none selected
        selected_projects = await tenant_db.azure_projects.find().to_list(None)

    total_synced = 0
    async with httpx.AsyncClient(timeout=20.0) as httpx_client:
        for project in selected_projects:
            proj_name = project.get("projectName")
            if not proj_name:
                continue

            # Execute WIQL query to get work item IDs
            wiql_url = f"{azure_url}/{proj_name}/_apis/wit/wiql?api-version=7.0"
            wiql_body = {
                "query": f"Select [System.Id] From WorkItems Where [System.TeamProject] = '{proj_name}' ORDER BY [System.ChangedDate] DESC"
            }
            try:
                wi_res = await httpx_client.post(wiql_url, json=wiql_body, headers=headers)
                if wi_res.status_code != 200:
                    continue
                wi_data = wi_res.json()
                work_items_refs = wi_data.get("workItems", [])[:200]
                if not work_items_refs:
                    continue

                ids = [str(item["id"]) for item in work_items_refs]

                # Fetch details for batch of IDs
                details_url = f"{azure_url}/_apis/wit/workitems?ids={','.join(ids)}&$expand=all&api-version=7.0"
                details_res = await httpx_client.get(details_url, headers=headers)
                if details_res.status_code != 200:
                    continue

                details_data = details_res.json()
                work_items = details_data.get("value", [])

                for item in work_items:
                    fields = item.get("fields", {})
                    wi_id = str(item.get("id"))
                    title = fields.get("System.Title", "")
                    work_item_type = fields.get("System.WorkItemType", "Task")
                    state = fields.get("System.State", "New")
                    assigned_to = fields.get("System.AssignedTo", {}).get("displayName") if isinstance(fields.get("System.AssignedTo"), dict) else str(fields.get("System.AssignedTo", ""))

                    issue_doc = {
                        "issueId": wi_id,
                        "key": f"{proj_name}-{wi_id}",
                        "summary": title,
                        "issueType": work_item_type,
                        "status": state,
                        "assignee": assigned_to,
                        "projectId": str(project.get("projectId")),
                        "projectName": proj_name,
                        "source": "azure-boards",
                        "updatedAt": datetime.utcnow()
                    }

                    await tenant_db.jira_issues.update_one(
                        {"issueId": wi_id, "source": "azure-boards"},
                        {"$set": issue_doc},
                        upsert=True
                    )
                    total_synced += 1
            except Exception as e:
                print(f"Error syncing project {proj_name}: {e}")

    return {"message": f"Synced {total_synced} work items from Azure Boards", "count": total_synced}
