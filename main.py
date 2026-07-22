from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import db, meta_db, client


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

@app.get("/users")
async def get_users():
    users = []

    async for user in db.users.find():
        user["_id"] = str(user["_id"])
        users.append(user)

    return users

@app.post("/companies")
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