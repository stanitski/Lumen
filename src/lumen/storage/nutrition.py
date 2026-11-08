"""Authoritative nutrition state and recoverable, four-column CSV projection.

The caller serializes access per user. One Lumen process owns this directory;
CSV is an export, not an input to the journal.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NutritionEstimate(JournalModel):
    description: str = Field(min_length=1, max_length=1000)
    full_portion_kcal: int = Field(gt=0, strict=True)
    consumed_fraction: float = Field(ge=0, le=1)

    @property
    def calories(self) -> int:
        return round(self.full_portion_kcal * self.consumed_fraction)


class Meal(JournalModel):
    id: str
    chat_id: str
    recorded_at: datetime
    eaten_at: datetime
    estimate: NutritionEstimate


class PendingMeal(JournalModel):
    id: str
    chat_id: str
    recorded_at: datetime
    updated_at: datetime
    original_text: str
    context: str
    estimate: NutritionEstimate | None = None
    proposed_date: str | None = None
    target_meal_id: str | None = None
    eaten_at: datetime | None = None


class NutritionReceipt(JournalModel):
    answer: str
    ref_id: str | None = None
    message_tag: str | None = None
    action: str | None = None
    callback_accepted: bool = False


class NutritionState(JournalModel):
    version: Literal[1] = 1
    user_id: str
    meals: list[Meal] = Field(default_factory=list)
    pending: dict[str, PendingMeal] = Field(default_factory=dict)
    # Keys are JSON [chat_id, message_id], so equal message IDs in other chats
    # cannot select a meal or collide with delivery deduplication.
    message_refs: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    receipts: dict[str, NutritionReceipt] = Field(default_factory=dict)


def message_key(chat_id: str, message_id: str) -> str:
    return json.dumps([chat_id, message_id], separators=(",", ":"))


class NutritionStore:
    COLUMNS = ("дата", "час", "калорії", "опис")

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def paths(self, user_id: str) -> tuple[Path, Path]:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)[:40] or "user"
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        stem = self.directory / f"{slug}-{digest}"
        return stem.with_suffix(".json"), stem.with_suffix(".csv")

    def load(self, user_id: str, *, repair_csv: bool = True) -> NutritionState:
        state_path, _ = self.paths(user_id)
        if not state_path.exists():
            return NutritionState(user_id=user_id)
        state = NutritionState.model_validate_json(state_path.read_bytes())
        if state.user_id != user_id:
            raise ValueError("Nutrition journal owner mismatch")
        # Repair the projection after interruption between JSON and CSV writes.
        if repair_csv:
            self.export(state)
        return state

    def save(self, state: NutritionState) -> None:
        state_path, _ = self.paths(state.user_id)
        self._atomic_write(state_path, state.model_dump_json(indent=2).encode("utf-8"))
        self.export(state)

    def export(self, state: NutritionState) -> None:
        _, csv_path = self.paths(state.user_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(self.COLUMNS)
        for meal in state.meals:
            # Quoting handles commas/newlines. A spreadsheet must not interpret
            # model/user-controlled descriptions as formulas when opening CSV.
            description = meal.estimate.description
            if description.lstrip().startswith(("=", "+", "-", "@")):
                description = "'" + description
            writer.writerow((meal.eaten_at.strftime("%Y-%m-%d"), meal.eaten_at.strftime("%H:%M"),
                             meal.estimate.calories, description))
        content = output.getvalue().encode("utf-8-sig")
        if not csv_path.exists() or csv_path.read_bytes() != content:
            self._atomic_write(csv_path, content)

    def read_period(
        self,
        user_id: str,
        start_date: date,
        end_date: date,
        timezone: tzinfo | None = None,
    ) -> "NutritionReadResult":
        """Read a user's journal for an inclusive local-date range.

        This method deliberately loads only the authoritative JSON and never
        repairs or writes the CSV projection.
        """
        if end_date < start_date:
            return NutritionReadResult("unavailable", "The requested date range is invalid.")
        try:
            state = self.load(user_id, repair_csv=False)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return NutritionReadResult("unavailable", "The nutrition journal is unavailable.")
        meals = []
        for meal in state.meals:
            eaten_at = meal.eaten_at
            if eaten_at.tzinfo is None:
                return NutritionReadResult("unavailable", "The nutrition journal contains an invalid timestamp.")
            if timezone is not None:
                eaten_at = eaten_at.astimezone(timezone)
            if start_date <= eaten_at.date() <= end_date:
                meals.append(meal)
        meals.sort(key=lambda meal: meal.eaten_at)
        total = sum(meal.estimate.calories for meal in meals)
        lines = [f"Nutrition journal for {start_date.isoformat()} through {end_date.isoformat()}.",
                 f"Meals: {len(meals)}; total calories: {total}."]
        for meal in meals:
            moment = meal.eaten_at.astimezone(timezone) if timezone is not None else meal.eaten_at
            lines.append(f"{moment.isoformat()}: {meal.estimate.calories} kcal — {meal.estimate.description}")
        return NutritionReadResult("available", "Nutrition journal read successfully.", "\n".join(lines), total, len(meals))

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class NutritionReadResult:
    """Read-only result suitable for external-context dispatch."""

    status: str
    detail: str
    context: str = ""
    total_calories: int = 0
    meal_count: int = 0
