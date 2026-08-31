from datetime import datetime
from bson import ObjectId
from database import client, db, meta_db


def get_tenant_db(company_name: str):
    db_name = company_name.lower().replace(" ", "_").strip()
    return client[db_name]


def to_object_id(val):
    if not val:
        return None
    if isinstance(val, ObjectId):
        return val
    val_str = str(val).strip()
    if ObjectId.is_valid(val_str):
        return ObjectId(val_str)
    return None


def sanitize_doc(doc: dict) -> dict:
    if not doc:
        return doc
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    for field in ["companyId", "projectId", "boardId"]:
        if field in doc and doc[field] is not None:
            doc[field] = str(doc[field])
    if "sprintId" in doc and isinstance(doc["sprintId"], list):
        doc["sprintId"] = [str(sid) if isinstance(sid, ObjectId) else sid for sid in doc["sprintId"]]
    
    # Virtual properties for UI compatibility if reading a PR document
    if "prId" in doc or "prCreatedBy" in doc:
        doc["author"] = doc.get("author") or doc.get("prCreatedBy")
        doc["repoName"] = doc.get("repoName") or doc.get("repo")
        if "reviewer" not in doc or not doc["reviewer"]:
            revs = [r.get("user") for r in doc.get("reviews", []) if r.get("user")]
            doc["reviewer"] = ", ".join(sorted(set(revs))) if revs else "Unassigned"
        if "daysOpen" not in doc or doc.get("daysOpen") is None:
            pr_created = doc.get("prCreatedAt")
            if isinstance(pr_created, datetime):
                doc["daysOpen"] = (datetime.utcnow() - pr_created.replace(tzinfo=None)).days
            else:
                doc["daysOpen"] = 0
    return doc


def parse_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        try:
            return datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val_str, fmt)
            except Exception:
                pass
    return None
