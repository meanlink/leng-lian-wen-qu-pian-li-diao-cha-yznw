from pydantic import BaseModel, Field
class ReportInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    job_id: int
    destination: str = Field(default='业务复核组', min_length=2, max_length=120)
    interval_seconds: int = Field(default=86400, ge=60, le=2592000)
    delay_seconds: int = Field(default=0, ge=0, le=2592000)
class ArchiveRepairInput(BaseModel):
    delivery_ids: list[int] = Field(min_length=1, max_length=200)
