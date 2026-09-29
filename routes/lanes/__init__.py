"""routes/lanes.py - Settings -> Models: lanes ("models"), job routing, answer check.

  GET    /control/lanes                   everything the page needs (plain-language)
  POST   /control/lanes                   add/edit a model (local: admin; cloud: own)
  DELETE /control/lanes?name=&move_to=    remove a model, moving its jobs elsewhere
  POST   /control/lanes/roles             map jobs to models (as_default: admin)
  POST   /control/lanes/{name}/test       one tiny request, plain-language result
  POST   /control/lanes/{name}/load       load an image/video model (sd.cpp) - never automatic
  POST   /control/lanes/{name}/stop       unload a local model now
  GET    /control/lanes/files             model files in Models/orchestrator (admin)
  POST   /control/verification            answer-check settings (per user)

Local models run on shared hardware, so creating/editing them needs
model.local.configure; cloud models and job choices are each user's own.
"""

from .helpers import (
    router,
    _LOCAL_PERM,
    _view,
    _FILES_CACHE,
    _helper_files,
    _err,
)
from .models import (
    LaneReq,
    RolesReq,
    MediaSettingsReq,
    VerifyReq,
)
from .endpoints import (
    lanes_get,
    lanes_files,
    lanes_save,
    lanes_delete,
    lanes_roles,
)
from .probe import (
    _plain_error,
    _silent_wav,
    _test_media,
    lanes_test,
)
from .loading import (
    _LOAD_TASKS,
    _bg_load,
    lanes_load,
    lanes_stop,
)
from .settings import (
    media_settings_save,
    verification_save,
)

__all__ = [
    "router",
    "_LOCAL_PERM",
    "_view",
    "_FILES_CACHE",
    "_helper_files",
    "_err",
    "LaneReq",
    "RolesReq",
    "MediaSettingsReq",
    "VerifyReq",
    "lanes_get",
    "lanes_files",
    "lanes_save",
    "lanes_delete",
    "lanes_roles",
    "_plain_error",
    "_silent_wav",
    "_test_media",
    "lanes_test",
    "_LOAD_TASKS",
    "_bg_load",
    "lanes_load",
    "lanes_stop",
    "media_settings_save",
    "verification_save",
]
