from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date

class LeadExtracted(BaseModel):
    phone: str
    name: Optional[str] = None
    level: Optional[str] = None
    city: Optional[str] = None
    status: str
    flagged: Optional[bool] = False

class LeadSubmit(BaseModel):
    leads: List[LeadExtracted]
    source_date: Optional[date] = None

class LeadResponse(BaseModel):
    id: str
    phone: str
    name: Optional[str]
    level: Optional[str]
    city: Optional[str]
    status: str
    original_agent: str
    current_agent: str
    swap_count: int
    submitted_at: datetime
    locked: bool
    is_blacklisted: bool
