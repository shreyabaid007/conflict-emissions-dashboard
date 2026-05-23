"""Emission category protocol and plugin registry.

Every emission category (oil-fuel-fire, structural-damage, etc.) implements
the ``EmissionCategory`` protocol. Categories are discovered at runtime via
``importlib.metadata`` entry points in the ``wced.categories`` group, so
third-party packages can register new categories without modifying WCED core.

Entry-point registration (in pyproject.toml)::

    [project.entry-points."wced.categories"]
    oil_fuel_fire = "wced.categories.oil_fuel_fire.category:OilFuelFireCategory"
"""
from __future__ import annotations

import importlib.metadata
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from wced.quantify.distribution import Distribution

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "wced.categories"


@dataclass(frozen=True)
class SourceSpec:
    """Declares a data source that a category requires for detection.

    The pipeline checks that all required sources are available before
    invoking a category's ``detect`` method.

    Parameters
    ----------
    name : str
        Machine-readable source identifier, e.g. "firms_viirs", "gdelt".
    description : str
        Human-readable explanation of what this source provides.
    required : bool
        If True, the pipeline must provide this source or skip the category.
        If False, the category degrades gracefully when the source is absent.
    """

    name: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class DetectionEvent:
    """A detected event produced by a category's ``detect`` method.

    Wraps category-specific detection data in a uniform envelope so the
    pipeline can pass it through verify and quantify without knowing the
    category's internals.

    Parameters
    ----------
    event_id : str
        Stable identifier for this detection within the category.
    category_id : str
        Which category produced this detection.
    data : dict[str, Any]
        Category-specific payload. The category's ``verify`` and ``quantify``
        methods know how to interpret this; the pipeline treats it as opaque.
    """

    event_id: str
    category_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying a single detection event.

    Parameters
    ----------
    event_id : str
        Which detection this verification applies to.
    verified : bool
        Whether the event passed verification (eligible for quantification).
    confidence_label : str
        One of the ConfidenceLabel values ("CONFIRMED", "VERIFIED", etc.).
    data : dict[str, Any]
        Category-specific verification details (S2 classification, corroboration
        matches, etc.).
    """

    event_id: str
    verified: bool
    confidence_label: str
    data: dict[str, Any]


@runtime_checkable
class EmissionCategory(Protocol):
    """Protocol that every emission category must implement.

    The pipeline calls these methods in order: ``required_sources`` to check
    prerequisites, ``detect`` to find events, ``verify`` to assign confidence,
    and ``quantify`` to produce emission distributions.
    """

    @property
    def id(self) -> str:
        """Unique machine-readable identifier for this category, e.g. ``"oil_fuel_fire"``."""
        ...

    @property
    def methodology_version(self) -> str:
        """Methodology version this category implements, e.g. ``"1.1.0"``."""
        ...

    def required_sources(self) -> list[SourceSpec]:
        """Declare which data sources this category needs."""
        ...

    def detect(self, ctx: dict[str, Any]) -> list[DetectionEvent]:
        """Run detection on ingested data.

        Parameters
        ----------
        ctx : dict[str, Any]
            Pipeline context containing ingested data keyed by source name.
            The category extracts what it needs based on ``required_sources()``.

        Returns
        -------
        list[DetectionEvent]
            Detected events ready for verification.
        """
        ...

    def verify(self, event: DetectionEvent, ctx: dict[str, Any]) -> VerificationResult:
        """Verify a single detected event and assign a confidence label.

        Parameters
        ----------
        event : DetectionEvent
            A detection from ``detect()``.
        ctx : dict[str, Any]
            Pipeline context (same as passed to ``detect``; may contain
            additional verification data like S2 chips or conflict events).

        Returns
        -------
        VerificationResult
        """
        ...

    def quantify(self, event: DetectionEvent, verification: VerificationResult) -> Distribution:
        """Produce an emission estimate for a verified event.

        Parameters
        ----------
        event : DetectionEvent
            The detection.
        verification : VerificationResult
            The verification result for this event.

        Returns
        -------
        Distribution
            Emission estimate in tCO2e.
        """
        ...


class CategoryRegistry:
    """Discovers and holds references to all registered EmissionCategory plugins.

    Categories are loaded lazily from ``importlib.metadata`` entry points
    in the ``wced.categories`` group. Each entry point must resolve to a
    callable that returns an ``EmissionCategory`` instance (typically a
    class with a no-arg constructor).

    Usage::

        registry = CategoryRegistry.discover()
        for cat in registry.all():
            print(cat.id, cat.methodology_version)
        oil = registry.get("oil_fuel_fire")
    """

    def __init__(self) -> None:
        self._categories: dict[str, EmissionCategory] = {}

    def register(self, category: EmissionCategory) -> None:
        """Register a category instance. Raises on duplicate id."""
        if category.id in self._categories:
            raise ValueError(
                f"Duplicate category id {category.id!r}: "
                f"already registered as {type(self._categories[category.id]).__name__}"
            )
        self._categories[category.id] = category

    def get(self, category_id: str) -> EmissionCategory:
        """Return a category by id. Raises KeyError if not found."""
        try:
            return self._categories[category_id]
        except KeyError:
            raise KeyError(
                f"Unknown category {category_id!r}; "
                f"registered: {sorted(self._categories)}"
            ) from None

    def all(self) -> list[EmissionCategory]:
        """Return all registered categories in registration order."""
        return list(self._categories.values())

    def ids(self) -> list[str]:
        """Return sorted list of registered category ids."""
        return sorted(self._categories)

    def __len__(self) -> int:
        return len(self._categories)

    def __contains__(self, category_id: str) -> bool:
        return category_id in self._categories

    @classmethod
    def discover(cls) -> CategoryRegistry:
        """Build a registry by loading all ``wced.categories`` entry points.

        Each entry point must be a callable (class or factory function) that
        accepts no arguments and returns an ``EmissionCategory`` instance.
        Entry points that fail to load are logged and skipped.

        When no entry points are found (common with editable installs that
        haven't rebuilt metadata), falls back to importing the built-in
        categories directly.
        """
        registry = cls()
        eps = importlib.metadata.entry_points()

        # Python 3.12+ returns a SelectableGroups; 3.9-3.11 returns a dict.
        if hasattr(eps, "select"):
            group_eps = list(eps.select(group=ENTRY_POINT_GROUP))
        else:
            group_eps = list(eps.get(ENTRY_POINT_GROUP, []))

        for ep in group_eps:
            try:
                factory = ep.load()
                instance = factory()
                if not isinstance(instance, EmissionCategory):
                    log.warning(
                        "category_registry.skip_non_conforming",
                        extra={"entry_point": ep.name, "type": type(instance).__name__},
                    )
                    continue
                registry.register(instance)
                log.info(
                    "category_registry.loaded",
                    extra={"id": instance.id, "entry_point": ep.name},
                )
            except Exception:
                log.exception(
                    "category_registry.load_failed",
                    extra={"entry_point": ep.name},
                )

        if len(registry) == 0:
            registry._load_builtins()

        return registry

    def _load_builtins(self) -> None:
        """Import built-in categories when entry-point discovery finds nothing."""
        builtins = [
            "wced.categories.oil_fuel_fire.category:OilFuelFireCategory",
        ]
        for spec in builtins:
            module_path, class_name = spec.rsplit(":", 1)
            try:
                import importlib
                mod = importlib.import_module(module_path)
                factory = getattr(mod, class_name)
                instance = factory()
                self.register(instance)
                log.info(
                    "category_registry.loaded_builtin",
                    extra={"id": instance.id, "spec": spec},
                )
            except Exception:
                log.exception(
                    "category_registry.builtin_load_failed",
                    extra={"spec": spec},
                )


_registry: CategoryRegistry | None = None


def get_registry() -> CategoryRegistry:
    """Return the singleton category registry, discovering on first call."""
    global _registry
    if _registry is None:
        _registry = CategoryRegistry.discover()
    return _registry


def reset_registry() -> None:
    """Clear the singleton registry. Primarily for testing."""
    global _registry
    _registry = None
