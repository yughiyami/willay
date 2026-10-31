"""Ports: the interfaces the application layer depends on."""

from willay.ports.agent import AgentModelPort, AgentTurn, StructuredExtractionPort, ToolCall
from willay.ports.hazard_data import HazardDataPort
from willay.ports.image import ImagePort, TranscriptionPort
from willay.ports.llm import LLMPort
from willay.ports.messaging import MessagingPort
from willay.ports.segmentation import SegmentationPort, VideoSegmentationPort
from willay.ports.site_repository import SiteRepositoryPort
from willay.ports.weather import WeatherPort

__all__ = [
    "AgentModelPort",
    "AgentTurn",
    "HazardDataPort",
    "ImagePort",
    "LLMPort",
    "MessagingPort",
    "SegmentationPort",
    "SiteRepositoryPort",
    "StructuredExtractionPort",
    "ToolCall",
    "TranscriptionPort",
    "VideoSegmentationPort",
    "WeatherPort",
]
