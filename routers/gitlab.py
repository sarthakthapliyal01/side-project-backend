from datetime import datetime
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from utils import get_tenant_db, sanitize_doc

router = APIRouter(tags=["GitLab Integration"])


class GitLabConnectionRequest(BaseModel):
    gitlab_url: Optional[str] = "https://gitlab.com"
    gitlab_owner: str
    gitlab_token: str


class SaveGitLabRequest(BaseModel):
    companyName: str
    gitlab_url: Optional[str] = "https://gitlab.com"
    gitlab_owner: str
    gitlab_token: str


def clean_url(url: str) -> str:
    url = (url or "https://gitlab.com").strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url


@router.post("/gitlab/test-connection")
async def test_gitlab_connection(data: GitLabConnectionRequest):
    base_url = clean_url(data.gitlab_url)
    token = data.gitlab_token.strip()
    owner = data.gitlab_owner.strip()

    header_options = [
        {"PRIVATE-TOKEN": token, "Accept": "application/json"},
        {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        for headers in header_options:
            # 1. Check self token endpoint
            try:
                self_res = await client.get(f"{base_url}/api/v4/personal_access_tokens/self", headers=headers)
                if self_res.status_code == 200:
                    token_info = self_res.json()
                    return {
                        "connected": True,
                        "owner": token_info.get("name") or owner or "GitLab Token",
                        "type": "Personal Access Token"
                    }
            except Exception:
                pass

            # 2. Check authenticated user endpoint
            try:
                user_res = await client.get(f"{base_url}/api/v4/user", headers=headers)
                if user_res.status_code == 200:
                    user_data = user_res.json()
                    return {
                        "connected": True,
                        "owner": user_data.get("username") or owner,
                        "name": user_data.get("name"),
                        "type": "User"
                    }
            except Exception:
                pass

            # 3. Check group/user if owner specified
            if owner:
                owner_slug = owner.lower().replace(" ", "-")
                for target_owner in [owner, owner_slug]:
                    try:
                        group_res = await client.get(f"{base_url}/api/v4/groups/{target_owner}", headers=headers)
                        if group_res.status_code == 200:
                            group_data = group_res.json()
                            return {
                                "connected": True,
                                "owner": group_data.get("path") or target_owner,
                                "name": group_data.get("name"),
                                "type": "Group"
                            }
                    except Exception:
                        pass

                    try:
                        u_res = await client.get(f"{base_url}/api/v4/users?username={target_owner}", headers=headers)
                        if u_res.status_code == 200 and isinstance(u_res.json(), list) and len(u_res.json()) > 0:
                            u_data = u_res.json()[0]
                            return {
                                "connected": True,
                                "owner": u_data.get("username") or target_owner,
                                "name": u_data.get("name"),
                                "type": "User"
                            }
                    except Exception:
                        pass

            # 4. Check member/accessible projects endpoint (works for Fine-grained PATs)
            for proj_url in [
                f"{base_url}/api/v4/projects?membership=true&per_page=1",
                f"{base_url}/api/v4/projects?per_page=1"
            ]:
                try:
                    proj_res = await client.get(proj_url, headers=headers)
                    if proj_res.status_code == 200:
                        return {
                            "connected": True,
                            "owner": owner or "GitLab",
                            "type": "Fine-Grained Token"
                        }
                except Exception:
                    pass

    raise HTTPException(
        status_code=400,
        detail="Unable to verify GitLab credentials. Please ensure your Personal Access Token has 'api' or 'read_api' & 'read_repository' permissions enabled."
    )


@router.post("/gitlab/save-connection")
async def save_gitlab_connection(data: SaveGitLabRequest):
    tenant_db = get_tenant_db(data.companyName)
    base_url = clean_url(data.gitlab_url)

    connection_data = {
        "integrationType": "gitlab",
        "gitlab_url": base_url,
        "gitlab_owner": data.gitlab_owner.strip(),
        "gitlab_token": data.gitlab_token.strip(),
        "status": "connected",
        "updatedAt": datetime.utcnow()
    }

    await tenant_db.connections.update_one(
        {"integrationType": "gitlab"},
        {"$set": connection_data},
        upsert=True
    )

    return {"message": "GitLab connection saved successfully"}


@router.get("/gitlab/connection/{company_name}")
async def get_gitlab_connection(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "gitlab"})
    if not connection:
        return {"connected": False}
    return {
        "connected": True,
        "gitlab_owner": connection.get("gitlab_owner"),
        "gitlab_url": connection.get("gitlab_url", "https://gitlab.com")
    }


@router.post("/gitlab/sync-repos/{company_name}")
async def sync_gitlab_repos(company_name: str):
    tenant_db = get_tenant_db(company_name)
    connection = await tenant_db.connections.find_one({"integrationType": "gitlab"})
    if not connection:
        raise HTTPException(status_code=400, detail="GitLab integration not configured.")

    base_url = clean_url(connection.get("gitlab_url", "https://gitlab.com"))
    token = connection.get("gitlab_token")
    owner = connection.get("gitlab_owner", "").strip()

    header_options = [
        {"PRIVATE-TOKEN": token},
        {"Authorization": f"Bearer {token}"}
    ]
    repos = []
    seen_ids = set()

    async with httpx.AsyncClient(timeout=20.0) as client:
        for headers in header_options:
            # 1. Fetch user/member projects
            urls = [
                f"{base_url}/api/v4/projects?membership=true&per_page=100",
            ]
            if owner:
                urls.extend([
                    f"{base_url}/api/v4/groups/{owner}/projects?per_page=100",
                    f"{base_url}/api/v4/users/{owner}/projects?per_page=100",
                ])

            for url in urls:
                try:
                    res = await client.get(url, headers=headers)
                    if res.status_code == 200 and isinstance(res.json(), list):
                        for p in res.json():
                            pid = str(p.get("id"))
                            if pid not in seen_ids:
                                seen_ids.add(pid)
                                repos.append({
                                    "id": pid,
                                    "name": p.get("name"),
                                    "fullName": p.get("path_with_namespace"),
                                    "webUrl": p.get("web_url"),
                                    "defaultBranch": p.get("default_branch", "main"),
                                    "source": "gitlab",
                                    "updatedAt": datetime.utcnow()
                                })
                except Exception:
                    pass

            if repos:
                break

    for repo in repos:
        await tenant_db.repositories.update_one(
            {"id": repo["id"], "source": "gitlab"},
            {"$set": repo},
            upsert=True
        )

    return {"message": f"Synced {len(repos)} GitLab repositories", "repositories": repos}


@router.get("/gitlab/repos/{company_name}")
async def get_gitlab_repos(company_name: str):
    tenant_db = get_tenant_db(company_name)
    repos = await tenant_db.repositories.find({"source": "gitlab"}).to_list(None)
    return {"repositories": [sanitize_doc(r) for r in repos]}
