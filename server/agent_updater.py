from fastapi import APIRouter

ota_router = APIRouter(prefix="/ota", tags=["ota"])

@ota_router.get("/health")
def ota_health():
    return {"status": "ok", "module": "agent_updater"}
