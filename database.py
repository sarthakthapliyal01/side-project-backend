from dotenv import load_dotenv
import os
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGO_URL = (
    os.getenv("MONGO_URL") or
    os.getenv("mongodb_url") or
    os.getenv("MONGODB_URL") or
    os.getenv("MONGO_URI") or
    os.getenv("mongodb_uri")
)

client = AsyncIOMotorClient(MONGO_URL)

meta_db = client["QMetrixMetaDB"]

db = client["side_project"]