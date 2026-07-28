from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import db, meta_db, client
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, EmailStr #better for taking input for jira like email api
from datetime import datetime #this is for jira 

class JiraConnectionRequest(BaseModel):
    jira_host: str
    jira_email: EmailStr
    jira_token: str

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

@app.post("/companies") # this is the part where different companies db are made 
async def create_company(company: dict):

    existing_company = await meta_db.companies.find_one(
        {"companyName": company["companyName"]}
    )

    print("Searching for:", company["companyName"])
    print("Found:", existing_company)

    if existing_company:
        return {"message": "Company already exists"}

    result = await meta_db.companies.insert_one(company)

    database_name = company["companyName"].lower().replace(" ", "_")
    tenant_db = client[database_name]

    print("Creating tenant DB:", company["companyName"])

    await tenant_db.company.insert_one(company)

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