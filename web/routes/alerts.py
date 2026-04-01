"""Alert history API.

Provides endpoints to query alert history and retrieve individual alert
details including full frame images.

Requirements: 13.5, 16.3
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from video.alert_system import AlertSystem

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class AlertSummary(BaseModel):
    """Compact alert representation for the history list."""

    id: str
    timestamp: float
    condition: str
    description: str
    frame_thumbnail: str = Field(
        description="Base64-encoded PNG thumbnail of the captured frame"
    )
    batched_conditions: Optional[list[str]] = None


class AlertDetail(BaseModel):
    """Full alert detail including the complete frame image."""

    id: str
    timestamp: float
    condition: str
    description: str
    frame_b64: str = Field(
        description="Base64-encoded full PNG frame image"
    )
    batched_conditions: Optional[list[str]] = None


class AlertHistoryResponse(BaseModel):
    alerts: list[AlertSummary]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_alert_system(request: Request) -> AlertSystem | None:
    return getattr(request.app.state, "alert_system", None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/alerts", response_model=AlertHistoryResponse)
async def get_alerts(request: Request, limit: int = 50):
    """Alert history with timestamps, descriptions, and frame thumbnails."""
    alert_system = _get_alert_system(request)

    if alert_system is None:
        return AlertHistoryResponse(alerts=[], total=0)

    records = await alert_system.get_history(limit=limit)

    alerts = [
        AlertSummary(
            id=r.id,
            timestamp=r.timestamp,
            condition=r.condition,
            description=r.description,
            frame_thumbnail=r.frame_b64,
            batched_conditions=r.batched_conditions,
        )
        for r in records
    ]

    return AlertHistoryResponse(alerts=alerts, total=len(alerts))


@router.get("/alerts/{alert_id}", response_model=AlertDetail)
async def get_alert(alert_id: str, request: Request):
    """Single alert detail with full frame image."""
    alert_system = _get_alert_system(request)

    if alert_system is None:
        raise HTTPException(status_code=503, detail="Alert system not initialised")

    records = await alert_system.get_history(limit=500)

    for r in records:
        if r.id == alert_id:
            return AlertDetail(
                id=r.id,
                timestamp=r.timestamp,
                condition=r.condition,
                description=r.description,
                frame_b64=r.frame_b64,
                batched_conditions=r.batched_conditions,
            )

    raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
