"""Ports: the interfaces the application layer depends on."""

from willay.ports.hazard_data import HazardDataPort
from willay.ports.image import ImagePort, TranscriptionPort
from willay.ports.llm import LLMPort
from willay.ports.messaging import MessagingPort
from willay.ports.segmentation import SegmentationPort
from willay.ports.site_repository import SiteRepositoryPort
from willay.ports.weather import WeatherPort

__all__ = [
    "HazardDataPort",
    "ImagePort",
    "LLMPort",
    "MessagingPort",
    "SegmentationPort",
    "SiteRepositoryPort",
    "TranscriptionPort",
    "WeatherPort",
]
