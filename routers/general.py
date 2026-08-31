from datetime import datetime
from fastapi import APIRouter, HTTPException
from database import db, meta_db, client
from models.board import Board

router = APIRouter(tags=["General"])


@router.get("/")
def root():
    return {"message": "Backend is working"}


@router.get("/test-db")
async def test_db():
    collections = await db.list_collection_names()
    return {"collections": collections}


@router.post("/users")
async def create_user(user: dict):
    existing_user = await db.users.find_one({"email": user.get("email")})
    if existing_user:
        return {"message": "User already exists"}
    result = await db.users.insert_one(user)
    return {"message": "User added", "id": str(result.inserted_id)}


@router.get("/users")
async def get_users():
    users = []
    async for user in db.users.find():
        user["_id"] = str(user["_id"])
        users.append(user)
    return users


@router.post("/companies")
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


@router.get("/boards/{company_name}")
async def get_boards(company_name: str):
    tenant_db = client[company_name]
    boards = []
    async for board in tenant_db.boards.find():
        board["_id"] = str(board["_id"])
        boards.append(board)
    return boards


@router.post("/boards/{company_name}")
async def create_board(company_name: str, board: Board):
    tenant_db = client[company_name]
    existing_board = await tenant_db.boards.find_one({"boardId": board.boardId})
    if existing_board:
        return {"message": "Board already exists"}

    board_data = board.model_dump()
    result = await tenant_db.boards.insert_one(board_data)
    return {"message": "Board created", "id": str(result.inserted_id)}
