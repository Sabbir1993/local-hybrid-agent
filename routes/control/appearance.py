"""Per-user Appearance & Themes settings (authenticated, scoped to the caller)."""
from fastapi import Depends
from pydantic import BaseModel

from core import user_preferences
from core.auth import Principal
from core.deps import get_current_user

from .base import router


class AppearanceBody(BaseModel):
    conversation_steps: str | None = None
    mode: str | None = None
    contrast: str | None = None
    font: str | None = None
    font_size: int | None = None
    theme: str | None = None
    background: str | None = None
    foreground: str | None = None
    accent: str | None = None


@router.get("/control/appearance")
async def get_appearance(user: Principal = Depends(get_current_user)):
    return user_preferences.get(user.id)


@router.put("/control/appearance")
async def set_appearance(body: AppearanceBody, user: Principal = Depends(get_current_user)):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    return user_preferences.save(user.id, values)
