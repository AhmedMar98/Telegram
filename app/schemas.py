"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)
    invite_code: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChannelCreate(BaseModel):
    tg_channel_id: str = Field(min_length=1, max_length=64)
    username: str | None = None
    title: str | None = None


class ChannelOut(BaseModel):
    id: int
    tg_channel_id: str
    username: str | None
    title: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class LinkOut(BaseModel):
    id: int
    channel_id: int
    url: str
    domain: str
    category: str
    confidence: float
    classified_by: str
    raw_text: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkOut]


class StatsResponse(BaseModel):
    total_links: int
    total_channels: int
    by_category: dict[str, int]
