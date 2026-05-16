from pydantic import BaseModel


class AuthSessionRequest(BaseModel):
    access_token: str


class StepUpRequest(BaseModel):
    password: str
