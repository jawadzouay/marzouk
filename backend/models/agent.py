from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class AgentCreate(BaseModel):
    name: str
    pin: str

class AgentLogin(BaseModel):
    name: str
    pin: str

class AgentResponse(BaseModel):
    id: str
    name: str
    is_active: bool
    created_at: datetime
