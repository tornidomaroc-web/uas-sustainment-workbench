"""A fleet: aircraft with their flight records. Synthetic by default, real where labelled."""

from .config import FleetConfig, load_config
from .model import Aircraft, Fleet

__all__ = ["Aircraft", "Fleet", "FleetConfig", "load_config"]
