from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from backend.routes import router

app = FastAPI(title="BillShield")
app.include_router(router, prefix="/api")
INDEX = Path(__file__).parent.parent / "frontend" / "index.html"


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/health")
def health():
    return {"status": "ok"}
