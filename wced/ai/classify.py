"""Two-path fire classifier: local SWIR heuristic + Claude vision fallback.

The local heuristic uses Sentinel-2 SWIR (B12) saturation and a SWIR/red
contrast ratio to label clearly-burning and clearly-cold scenes without an
LLM call. Ambiguous chips — high SWIR but no strong contrast, or
mid-saturation values — are escalated to ``classify_fire_with_ai`` which
sends an RGB+SWIR composite PNG to Claude alongside facility metadata.

Both paths return a ``FireClassification`` and record a ``ProvenanceRecord``
in the supplied store. AI calls additionally record the underlying Claude
``Source`` so the prompt/model hash is auditable.

Methodology reference: methodology/v1.0.pdf §4.3 — "Optical Verification and
False-Positive Suppression".
"""
from __future__ import annotations

import base64
import enum
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import numpy as np
import xarray as xr
from pydantic import BaseModel, ConfigDict, Field

try:  # pragma: no cover - import-time only
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

from wced.ai.claude_client import AnthropicClient
from wced.detect.hotspot import CandidateFireEvent
from wced.models.facility import Facility
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord, Source
from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)

PROMPT_PATH: Final[Path] = Path(__file__).parent / "prompts" / "classify_fire.txt"
PROMPT_VERSION: Final[str] = "classify_fire/v1.0"
HEURISTIC_VERSION: Final[str] = "swir_heuristic/v1.0"

# Heuristic thresholds — methodology/v1.0.pdf §4.3, Table 4.
#
# B12 surface reflectance is scaled to [0, 1] by Sentinel2Connector
# (DN / 10 000). Initial values below are seed defaults from Murphy et al.
# 2016 ("HOTMAP" SWIR fire index, B12 ≥ 0.45 typical for active flame) and
# Schroeder et al. 2016 (S2-based fire detection, SWIR/red contrast ≥ ~2.5
# separates flame from sunlit bare ground at ~10 m resolution).
#
# **These are seed values, not validated thresholds.** Phase 5 must tune
# them against Oregon State damage-portal ground truth and a Pars-Refinery
# steady-state flaring baseline. Tuning targets:
#   - SWIR_SATURATION_THRESHOLD: lower bound on B12 reflectance that
#     reliably indicates combustion-class radiance.
#   - SWIR_OVER_RED_RATIO_FIRE: contrast above which the spectral shape
#     is fire-like (high SWIR, low/normal red).
#   - SWIR_OVER_RED_RATIO_COLD: contrast below which the chip is cold
#     (no anomaly).
SWIR_SATURATION_THRESHOLD: Final[float] = 0.45
SWIR_OVER_RED_RATIO_FIRE: Final[float] = 2.5
SWIR_OVER_RED_RATIO_COLD: Final[float] = 1.1

# Escalation gate. A chip is *only* answered by the heuristic alone when its
# assigned confidence falls outside (lo, hi); anything in between escalates
# to the vision model. `_heuristic_label` assigns three confidence anchors:
#   * 0.90 — CONFIRMED_FIRE   (both SWIR + ratio above fire thresholds)
#   * 0.85 — FALSE_POSITIVE   (both SWIR + ratio below cold thresholds)
#   * 0.40 — AMBIGUOUS        (everything else, including all flaring-like cases)
#   * 0.20 — AMBIGUOUS        (chip missing B12 entirely)
# With the band (0.15, 0.85), the 0.40 and 0.20 anchors escalate while the
# 0.85 and 0.90 anchors short-circuit. Widening the band saves API spend at
# the cost of false negatives; narrowing it sends more chips to Claude.
# **Tune in Phase 5 once we have a labelled escalation-rate target.**
HEURISTIC_CONFIDENT_BAND: Final[tuple[float, float]] = (0.15, 0.85)


class FireLabel(str, enum.Enum):
    """Mutually exclusive verification outcomes for a candidate fire."""

    CONFIRMED_FIRE = "CONFIRMED_FIRE"
    GAS_FLARING = "GAS_FLARING"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    AMBIGUOUS = "AMBIGUOUS"


class FireClassification(BaseModel):
    """Structured verdict from either the local heuristic or the AI path.

    Parameters
    ----------
    label : FireLabel
        Final classification.
    confidence : float
        Subjective confidence in [0, 1]. The local heuristic uses fixed
        anchor values per label; the AI path produces a model-supplied score.
    rationale : str
        Short human-readable justification. For the heuristic this is the
        triggering rule; for AI it is Claude's checklist answers.
    provenance_id : UUID
        ID of the ``ProvenanceRecord`` documenting this classification.
    """

    model_config = ConfigDict(frozen=True)

    label: FireLabel
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    provenance_id: UUID = Field(default_factory=uuid4)


class _AIVerdict(BaseModel):
    """Schema Claude must populate via the structured tool call."""

    label: FireLabel
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------


def _band_array(chip: xr.Dataset, name: str) -> np.ndarray | None:
    if name not in chip.data_vars:
        return None
    arr = np.asarray(chip[name].values, dtype="float32")
    return arr if arr.size else None


def _heuristic_metrics(chip: xr.Dataset) -> dict[str, float]:
    """Compute SWIR-saturation and SWIR/red contrast metrics for the chip."""
    swir = _band_array(chip, "B12")
    red = _band_array(chip, "B04")
    if swir is None:
        return {"swir_peak": float("nan"), "swir_mean": float("nan"), "swir_over_red": float("nan")}
    swir = swir[np.isfinite(swir)]
    if swir.size == 0:
        return {"swir_peak": 0.0, "swir_mean": 0.0, "swir_over_red": 0.0}
    swir_peak = float(np.nanmax(swir))
    swir_mean = float(np.nanmean(swir))
    if red is None:
        return {"swir_peak": swir_peak, "swir_mean": swir_mean, "swir_over_red": float("nan")}
    red_finite = red[np.isfinite(red)]
    red_ref = float(np.nanmedian(red_finite)) if red_finite.size else 0.0
    swir_over_red = swir_peak / red_ref if red_ref > 1e-6 else float("inf")
    return {
        "swir_peak": swir_peak,
        "swir_mean": swir_mean,
        "swir_over_red": swir_over_red,
    }


def _heuristic_label(metrics: dict[str, float]) -> tuple[FireLabel, float, str]:
    """Map heuristic metrics to a (label, confidence, rationale) triple."""
    swir_peak = metrics["swir_peak"]
    ratio = metrics["swir_over_red"]

    if not np.isfinite(swir_peak):
        return (
            FireLabel.AMBIGUOUS,
            0.2,
            "SWIR (B12) band not available in chip; cannot apply heuristic.",
        )

    if swir_peak >= SWIR_SATURATION_THRESHOLD and ratio >= SWIR_OVER_RED_RATIO_FIRE:
        return (
            FireLabel.CONFIRMED_FIRE,
            0.9,
            f"SWIR peak {swir_peak:.3f} ≥ {SWIR_SATURATION_THRESHOLD} and "
            f"SWIR/red ratio {ratio:.2f} ≥ {SWIR_OVER_RED_RATIO_FIRE}: "
            "saturated thermal source with strong fire spectral signature.",
        )

    if swir_peak < SWIR_SATURATION_THRESHOLD * 0.4 and ratio < SWIR_OVER_RED_RATIO_COLD:
        return (
            FireLabel.FALSE_POSITIVE,
            0.85,
            f"SWIR peak {swir_peak:.3f} below saturation floor and SWIR/red "
            f"ratio {ratio:.2f} < {SWIR_OVER_RED_RATIO_COLD}: no thermal "
            "anomaly in chip.",
        )

    return (
        FireLabel.AMBIGUOUS,
        0.4,
        f"SWIR peak {swir_peak:.3f} and SWIR/red ratio {ratio:.2f} fall in "
        "the indeterminate band; escalate to vision model.",
    )


def _is_confident(confidence: float) -> bool:
    lo, hi = HEURISTIC_CONFIDENT_BAND
    return confidence <= lo or confidence >= hi


def _record_heuristic_provenance(
    *,
    candidate: CandidateFireEvent,
    metrics: dict[str, float],
    label: FireLabel,
    confidence: float,
    rationale: str,
    store: ProvenanceStore,
) -> UUID:
    confidence_label = (
        ConfidenceLabel.REPORTED
        if label is FireLabel.CONFIRMED_FIRE and confidence >= 0.85
        else ConfidenceLabel.SUSPECTED
    )
    rec = ProvenanceRecord(
        produced_by="wced.ai.classify",
        inputs=[candidate.provenance_id],
        method=HEURISTIC_VERSION,
        parameters={
            "swir_saturation_threshold": SWIR_SATURATION_THRESHOLD,
            "swir_over_red_ratio_fire": SWIR_OVER_RED_RATIO_FIRE,
            "swir_over_red_ratio_cold": SWIR_OVER_RED_RATIO_COLD,
            "metrics": metrics,
            "label": label.value,
            "confidence": confidence,
        },
        produced_at=datetime.now(tz=UTC),
        confidence_label=confidence_label,
        notes=rationale,
    )
    return store.record_provenance(rec)


# ---------------------------------------------------------------------------
# AI path: render chip + send to Claude
# ---------------------------------------------------------------------------


def _stretch_uint8(arr: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0) -> np.ndarray:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype="uint8")
    lo = float(np.percentile(finite, lo_pct))
    hi = float(np.percentile(finite, hi_pct))
    if hi <= lo:
        hi = lo + 1e-6
    scaled = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    return (np.nan_to_num(scaled) * 255.0).astype("uint8")


def _render_composite_png(chip: xr.Dataset) -> bytes:
    """Render an RGB+SWIR composite as a side-by-side PNG byte string.

    Left panel: true-colour B04/B03/B02. Right panel: SWIR-1 (B12) as a
    single-band greyscale, percentile-stretched so flare/fire pixels visibly
    saturate.
    """
    red = _band_array(chip, "B04")
    green = _band_array(chip, "B03")
    blue = _band_array(chip, "B02")
    swir = _band_array(chip, "B12")

    if red is None or green is None or blue is None or swir is None:
        raise ValueError(
            "Chip is missing one of B04/B03/B02/B12; cannot render composite."
        )

    rgb = np.stack(
        [_stretch_uint8(red), _stretch_uint8(green), _stretch_uint8(blue)],
        axis=-1,
    )
    swir_u8 = _stretch_uint8(swir)
    swir_rgb = np.stack([swir_u8, swir_u8, swir_u8], axis=-1)

    h = max(rgb.shape[0], swir_rgb.shape[0])
    w = rgb.shape[1] + swir_rgb.shape[1]
    canvas = np.zeros((h, w, 3), dtype="uint8")
    canvas[: rgb.shape[0], : rgb.shape[1], :] = rgb
    canvas[: swir_rgb.shape[0], rgb.shape[1] :, :] = swir_rgb

    if Image is None:
        raise RuntimeError(
            "Pillow is required to render composite PNGs for the AI path."
        )
    buf = io.BytesIO()
    Image.fromarray(canvas, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _facility_block(facility: Facility) -> str:
    return json.dumps(
        {
            "name": facility.name,
            "type": facility.facility_type.value,
            "country": facility.country,
            "capacity_barrels": facility.capacity_barrels,
            "operator": facility.operator,
        },
        indent=2,
    )


def _candidate_block(candidate: CandidateFireEvent) -> str:
    return json.dumps(
        {
            "centroid_lat": candidate.centroid_lat,
            "centroid_lon": candidate.centroid_lon,
            "first_detected_at": candidate.first_detected_at.isoformat(),
            "last_detected_at": candidate.last_detected_at.isoformat(),
            "peak_frp_mw": candidate.peak_frp_mw,
            "mean_frp_mw": candidate.mean_frp_mw,
            "n_overpasses": candidate.n_overpasses,
            "n_hotspots": len(candidate.hotspots),
        },
        indent=2,
    )


def _heuristic_block(
    metrics: dict[str, float], label: FireLabel, confidence: float, rationale: str
) -> str:
    return json.dumps(
        {
            "metrics": metrics,
            "label": label.value,
            "confidence": confidence,
            "rationale": rationale,
        },
        indent=2,
    )


def _build_prompt(
    *,
    facility: Facility,
    candidate: CandidateFireEvent,
    metrics: dict[str, float],
    heuristic_label: FireLabel,
    heuristic_confidence: float,
    heuristic_rationale: str,
    png_bytes: bytes,
) -> list[dict[str, Any]]:
    template = PROMPT_PATH.read_text()
    text = template.format(
        facility_block=_facility_block(facility),
        candidate_block=_candidate_block(candidate),
        heuristic_block=_heuristic_block(
            metrics, heuristic_label, heuristic_confidence, heuristic_rationale
        ),
    )
    encoded = base64.standard_b64encode(png_bytes).decode("ascii")
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": encoded,
            },
        },
        {"type": "text", "text": text},
    ]


def _ai_classify(
    *,
    chip: xr.Dataset,
    candidate: CandidateFireEvent,
    facility: Facility,
    metrics: dict[str, float],
    heuristic_label: FireLabel,
    heuristic_confidence: float,
    heuristic_rationale: str,
    store: ProvenanceStore,
    client: AnthropicClient,
    model: str | None,
) -> FireClassification:
    png_bytes = _render_composite_png(chip)
    prompt = _build_prompt(
        facility=facility,
        candidate=candidate,
        metrics=metrics,
        heuristic_label=heuristic_label,
        heuristic_confidence=heuristic_confidence,
        heuristic_rationale=heuristic_rationale,
        png_bytes=png_bytes,
    )
    verdict = client.call(
        prompt,
        model=model,
        temperature=0.0,
        max_tokens=1024,
        response_model=_AIVerdict,
    )
    ai_source = client.last_source
    inputs: list[UUID] = [candidate.provenance_id]
    if ai_source is not None:
        store.record_source(ai_source)
        inputs.append(ai_source.id)

    confidence_label = (
        ConfidenceLabel.REPORTED
        if verdict.label is FireLabel.CONFIRMED_FIRE and verdict.confidence >= 0.85
        else ConfidenceLabel.SUSPECTED
    )

    rec = ProvenanceRecord(
        produced_by="wced.ai.classify",
        inputs=inputs,
        method=f"vision_classify/{PROMPT_VERSION}",
        parameters={
            "prompt_version": PROMPT_VERSION,
            "model": (model or client._settings.anthropic_default_model),
            "heuristic_metrics": metrics,
            "heuristic_label": heuristic_label.value,
            "heuristic_confidence": heuristic_confidence,
            "ai_label": verdict.label.value,
            "ai_confidence": verdict.confidence,
        },
        produced_at=datetime.now(tz=UTC),
        confidence_label=confidence_label,
        notes=verdict.rationale,
    )
    pid = store.record_provenance(rec)
    return FireClassification(
        label=verdict.label,
        confidence=verdict.confidence,
        rationale=verdict.rationale,
        provenance_id=pid,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_fire(
    s2_chip: xr.Dataset,
    candidate: CandidateFireEvent,
    facility: Facility,
    *,
    store: ProvenanceStore,
    client: AnthropicClient | None = None,
    model: str | None = None,
    force_ai: bool = False,
) -> FireClassification:
    """Classify a fire candidate using the local heuristic, escalating if unsure.

    Parameters
    ----------
    s2_chip : xr.Dataset
        Sentinel-2 chip from ``Sentinel2Connector.fetch_chip``. Must contain
        the B04, B03, B02, B12 bands (scaled to [0, 1] surface reflectance).
    candidate : CandidateFireEvent
        The clustered fire candidate to verify.
    facility : Facility
        Facility the candidate has been attributed to. Provides type and
        operator context for the AI prompt.
    store : ProvenanceStore
        Receives the ProvenanceRecord for whichever path produces the final
        verdict (and, for the AI path, the Claude DERIVED Source).
    client : AnthropicClient or None
        Anthropic wrapper. Constructed lazily only when the AI path is
        actually taken (the heuristic-only path requires no API key).
    model : str or None
        Override Claude model id. Defaults to the client's configured default.
    force_ai : bool
        If True, always run the AI path even when the heuristic is confident.
        Used for spot-audits.

    Returns
    -------
    FireClassification
        Final verdict with provenance_id pointing at the recorded
        ProvenanceRecord.
    """
    metrics = _heuristic_metrics(s2_chip)
    h_label, h_conf, h_rationale = _heuristic_label(metrics)

    if not force_ai and _is_confident(h_conf):
        pid = _record_heuristic_provenance(
            candidate=candidate,
            metrics=metrics,
            label=h_label,
            confidence=h_conf,
            rationale=h_rationale,
            store=store,
        )
        log.info(
            "classify_fire: heuristic verdict candidate=%s label=%s confidence=%.2f",
            candidate.id,
            h_label.value,
            h_conf,
        )
        return FireClassification(
            label=h_label,
            confidence=h_conf,
            rationale=h_rationale,
            provenance_id=pid,
        )

    log.info(
        "classify_fire: escalating to AI for candidate=%s heuristic=%s/%.2f",
        candidate.id,
        h_label.value,
        h_conf,
    )
    ai_client = client or AnthropicClient()
    return _ai_classify(
        chip=s2_chip,
        candidate=candidate,
        facility=facility,
        metrics=metrics,
        heuristic_label=h_label,
        heuristic_confidence=h_conf,
        heuristic_rationale=h_rationale,
        store=store,
        client=ai_client,
        model=model,
    )
