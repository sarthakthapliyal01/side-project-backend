import re
from datetime import datetime
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import meta_db
from models.pull_request import PullRequest
from utils import get_tenant_db, to_object_id, sanitize_doc

router = APIRouter(tags=["GitHub Integration"])


class GitHubConnectionRequest(BaseModel):
    github_owner: str
    github_token: str


class SaveGitHubRequest(BaseModel):
    companyName: str
    github_owner: str
    github_token: str


@router.post("/github/test-connection")
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


@router.post("/github/save-connection")
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


@router.get("/github/connection/{company_name}")
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


@router.get("/github/repos/{company_name}")
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


@router.post("/github/sync-repos/{company_name}")
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


@router.post("/github/sync-all/{company_name}")
async def sync_all_github_data(company_name: str):
    repos_res = await sync_github_repos(company_name)
    prs_res = await sync_github_prs(company_name)
    return {
        "message": "GitHub repositories and PRs synced successfully",
        "totalReposSynced": repos_res.get("totalSynced", 0),
        "totalPRsSynced": prs_res.get("totalSynced", 0)
    }


@router.get("/github/db-repos/{company_name}")
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


@router.get("/github/prs/{company_name}")
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


@router.get("/github/pr-details/{company_name}/{pr_id:path}")
async def get_github_pr_details(company_name: str, pr_id: str):
    tenant_db = get_tenant_db(company_name)
    pr = await tenant_db.github_prs.find_one({"$or": [{"prId": pr_id}, {"prId": f"#{pr_id}"}]})
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")
    return sanitize_doc(pr)


@router.post("/github/prs/{company_name}")
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


@router.post("/github/sync-prs/{company_name}")
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
