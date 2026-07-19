from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import db

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

@app.get("/api/message")
def get_message():
    return {"message": "Hello from FastAPI"}

@app.get("/test-db")
async def test_db():
    collections = await db.list_collection_names()
    return {"collections": collections}

@app.post("/users")
async def create_user(user: dict):
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