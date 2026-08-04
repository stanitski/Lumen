# Nutrition Activity Handoff

> **Статус: historical.** Описаний `nutrition_tracking`/`nutrition_summary`
> skill runtime видалено з поточного Lumen. Файли `src/lumen/tasks/executor.py`
> і відповідний test більше не існують. Поточні межі дивись у
> `docs/PROJECT.md` та `docs/HANDOVER.md`.

## What changed

- `nutrition_tracking` recognizes workouts and explicitly burned calories as physical activity.
- A workout is stored separately by date and does not become breakfast, lunch, dinner, snack, or drink.
- `nutrition_summary` adds saved activity for the selected day without asking the user to enter it again.
- Daily steps use the sensor alias from memory first, then read its current state from Home Assistant.

## Files changed

- `src/lumen/data/system_skills.json`
- `src/lumen/tasks/executor.py`
- `tests/test_pending_entity_clarification.py`

## Risks

- Workout detection depends on the model receiving an explicit calorie value; ambiguous messages should not be treated as a reliable activity entry.
- The steps result depends on a valid `device_alias` for the steps sensor; without one, Lumen keeps the existing text-search fallback.
- Activity records expire after 30 days, like meal records.

## How to test

```powershell
python -m unittest tests.test_pending_entity_clarification.PendingEntityClarificationTest.test_nutrition_training_is_stored_as_activity_not_snack tests.test_pending_entity_clarification.PendingEntityClarificationTest.test_nutrition_summary_aggregates_stored_training_without_reentry tests.test_pending_entity_clarification.PendingEntityClarificationTest.test_daily_steps_uses_memory_alias_and_live_state
```

Expected result: `Ran 3 tests ... OK`.

## What remains

- Run one live check with Ollama and Home Assistant after restart: send a workout with explicit calories, then request the day summary and confirm the configured steps sensor returns its live state.
