"""bambu-prep: agent-driven print prep for Bambu Lab printers."""

from bambu_prep.ams import (
    AMSError,
    TrayInfo,
    filament_suffix_for,
    get_ams_state,
)
from bambu_prep.config import Behavior, Config, Paths, Printer, load_config
from bambu_prep.meshes import MeshError, ScaledMesh, prescale
from bambu_prep.plate import (
    CliInput,
    PlateItem,
    PrepareError,
    PrepareResult,
    prepare_plate,
)
from bambu_prep.profiles import Profile, ProfileError, list_profiles, resolve

__version__ = "0.1.0"

__all__ = [
    "AMSError",
    "Behavior",
    "CliInput",
    "Config",
    "MeshError",
    "Paths",
    "PlateItem",
    "PrepareError",
    "PrepareResult",
    "Printer",
    "Profile",
    "ProfileError",
    "ScaledMesh",
    "TrayInfo",
    "__version__",
    "filament_suffix_for",
    "get_ams_state",
    "list_profiles",
    "load_config",
    "prepare_plate",
    "prescale",
    "resolve",
]
