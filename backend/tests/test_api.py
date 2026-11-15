from fastapi.testclient import TestClient
from app.api import app

client = TestClient(app)


