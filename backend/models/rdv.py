from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class RDVCreate(BaseModel):
    lead_id: str
    rdv_date: datetime

class RDVUpdate(BaseModel):
    status: str  # 'showed_up' or 'no_show'

class RDVResponse(BaseModel):
    id: str
    lead_id: str
    agent_id: str
    rdv_date: datetime
    status: str
    created_at: datetime
