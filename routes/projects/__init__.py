"""routes/projects - Projects, sessions, messages, and filesystem browsing endpoints."""

from fastapi import APIRouter

from .companion_endpoints import (
    browse_folder,
    companion_status,
    fs_browse,
    fs_mkdir,
    list_user_devices,
    rename_device,
    router as companion_router,
)
from .helpers import _ask_directory_native
from .models import (
    BrowseFolderReq,
    MessageReq,
    MkdirReq,
    ProjectReq,
    RenameDeviceReq,
    SessionReq,
    UpdateWorkspaceReq,
)
from .project_endpoints import (
    activate_project,
    create_project,
    delete_project,
    list_projects,
    router as project_router,
    update_project_workspace,
)
from .session_endpoints import (
    create_session,
    delete_session,
    get_messages,
    list_sessions,
    post_message,
    router as session_router,
    update_session,
)

router = APIRouter(tags=["projects"])
router.include_router(companion_router)
router.include_router(project_router)
router.include_router(session_router)

__all__ = [
    "router",
    # models
    "ProjectReq", "UpdateWorkspaceReq", "SessionReq", "BrowseFolderReq",
    "MkdirReq", "MessageReq", "RenameDeviceReq",
    # helpers
    "_ask_directory_native",
    # companion/fs/device endpoints
    "companion_status", "browse_folder", "fs_browse", "fs_mkdir", "rename_device", "list_user_devices", "companion_shell",
    # project endpoints
    "list_projects", "create_project", "update_project_workspace", "delete_project", "activate_project",
    # session endpoints
    "list_sessions", "create_session", "update_session", "delete_session",
    "get_messages", "post_message",
]
