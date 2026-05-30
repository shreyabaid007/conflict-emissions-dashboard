"""Database repositories — one per aggregate root."""
from wced.db.repositories.facility import PostgisFacilityRepository
from wced.db.repositories.fire_event import FireEventRepository
from wced.db.repositories.emission import EmissionEstimateRepository
from wced.db.repositories.provenance import ProvenanceRepository
from wced.db.repositories.damage import DamageAssessmentRepository
from wced.db.repositories.editorial import EditorialActionRepository
from wced.db.repositories.pipeline import PipelineRunRepository
from wced.db.repositories.ingestion import FirmsDetectionRepository, AcledEventRepository, S2ChipRepository
from wced.db.repositories.validation import ValidationReportRepository

__all__ = [
    "PostgisFacilityRepository",
    "FireEventRepository",
    "EmissionEstimateRepository",
    "ProvenanceRepository",
    "DamageAssessmentRepository",
    "EditorialActionRepository",
    "PipelineRunRepository",
    "FirmsDetectionRepository",
    "AcledEventRepository",
    "S2ChipRepository",
    "ValidationReportRepository",
]
