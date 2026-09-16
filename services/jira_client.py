import httpx
from fastapi import HTTPException


async def get_jira_connection_credentials(tenant_db):
    """
    Fetch Jira credentials for the given tenant database.
    """
    connection = await tenant_db.connections.find_one({"integrationType": "jira"})
    if not connection:
        raise HTTPException(status_code=404, detail="Jira connection not found")

    raw_host = connection.get("jira_host", "")
    jira_host = raw_host.replace(".atlassian.net", "").replace("https://", "").strip()
    jira_email = connection.get("jira_email", "").strip()
    jira_token = connection.get("jira_token", "").strip()

    auth = (jira_email, jira_token)
    headers = {"Accept": "application/json"}
    base_url = f"https://{jira_host}.atlassian.net"

    return {
        "jira_host": jira_host,
        "jira_email": jira_email,
        "jira_token": jira_token,
        "auth": auth,
        "headers": headers,
        "base_url": base_url,
    }


async def test_jira_credentials(jira_host: str, jira_email: str, jira_token: str):
    """
    Test Jira credentials against Atlassian REST API.
    """
    clean_host = jira_host.replace(".atlassian.net", "").replace("https://", "").strip()
    url = f"https://{clean_host}.atlassian.net/rest/api/3/project"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            auth=(jira_email, jira_token),
            headers={"Accept": "application/json"}
        )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid Jira credentials")

    return response.json()
