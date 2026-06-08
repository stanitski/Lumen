# Home Assistant Integration

This project already exposes Home Assistant-friendly endpoints:

- `POST /assist/process`
- `POST /assist/confirm`
- `POST /admin/bootstrap-home-assistant`
- `GET /home-assistant/snapshot`

## Minimal Live Flow

1. Start `LUMEN` on a reachable host, for example `http://10.0.0.160:8010`.
2. Set `HOME_ASSISTANT_TOKEN` in `LUMEN` so it can read entities and execute allowlisted services after confirmation.
3. Call `POST /admin/bootstrap-home-assistant` once to index scripts, scenes, and input booleans.

## Example REST Command

```yaml
rest_command:
  lumen_assist:
    url: "http://10.0.0.100:8010/assist/process"
    method: POST
    timeout: 120
    content_type: "application/json"
    payload: >
      {
        "text": {{ text | tojson }},
        "conversation_id": {{ conversation_id | tojson }},
        "user_id": {{ user_id | default("home-assistant", true) | tojson }},
        "session_id": {{ conversation_id | tojson }},
        "language": "uk",
        "exposed_entities": {{ exposed_entities | default([], true) | tojson }}
      }
```

In Home Assistant automations/scripts, call it as an action with
`response_variable`, for example:

```yaml
- action: rest_command.lumen_assist
  data:
    text: "{{ trigger.event.data.text }}"
    conversation_id: "telegram:{{ trigger.event.data.chat_id }}"
    user_id: "{{ trigger.event.data.user_id | default(trigger.event.data.chat_id) }}"
    exposed_entities:
      - input_boolean.guest_mode
      - input_boolean.sleeping
      - script.turnoffeverything
  response_variable: lumen_reply
```

For cold starts, `timeout: 120` is recommended because Home Assistant defaults
to 10 seconds for `rest_command`, while Ollama may need noticeably longer on the
first request after startup or after unloading a model.

`lumen_reply["content"]` then contains the parsed JSON response from `LUMEN`,
including:

- `speech`
- `requires_confirmation`
- `action_id`
- `action_label`
- `data.action_proposal`

For normal chat, `LUMEN` does not fetch the live Home Assistant state. For
questions that look like they are about the current home state, `LUMEN` now
queries Home Assistant directly and injects a compact live summary into the
model context. No extra YAML block is required for that.

If your Telegram bridge uses `parse_mode: markdownv2`, `LUMEN` now escapes
Assist `speech` output for Telegram `MarkdownV2`, so ordinary punctuation and
entity ids should not break message delivery.

## Example Manual Test

Developer Tools -> Actions -> `rest_command.lumen_assist`

```yaml
text: "Увімкни гостьовий режим"
conversation_id: "ha-manual-test-1"
exposed_entities:
  - input_boolean.guest_mode
  - script.turnoffeverything
```

## Confirmation

If `LUMEN` returns `requires_confirmation: true`, keep the returned `action_id`
and send it to:

```text
POST /assist/confirm
```

Body:

```json
{
  "action_id": "PUT_ACTION_ID_HERE",
  "confirmed": true,
  "conversation_id": "ha-manual-test-1",
  "user_id": "home-assistant"
}
```

Example Home Assistant action:

```yaml
- action: rest_command.lumen_assist_confirm
  data:
    action_id: "{{ action_id }}"
    confirmed: true
    conversation_id: "telegram:{{ trigger.event.data.chat_id }}"
    user_id: "{{ trigger.event.data.user_id | default(trigger.event.data.chat_id) }}"
  response_variable: lumen_confirm_reply
```

## Telegram Via Home Assistant

If you want to keep your proven Home Assistant Telegram bridge, the recommended
flow is:

1. `telegram_text` event arrives in Home Assistant.
2. Home Assistant wakes WOODY if `binary_sensor.10_0_0_100` is `off`.
3. Home Assistant posts the message to `POST /assist/process`.
4. Home Assistant sends `speech` back through `telegram_bot.send_message`.
5. If `requires_confirmation` is `true`, Home Assistant appends:
   `/confirm ACTION_ID` and `/cancel ACTION_ID`.
6. A second Telegram message with `/confirm ACTION_ID` or `/cancel ACTION_ID`
   is forwarded to `POST /assist/confirm`.
7. Home Assistant restores the previous WOODY sleep state.

The full example package for this pattern lives in
`docs/home-assistant-package-example.yaml`.

## Notes

- This keeps action execution behind an explicit confirmation barrier.
- `LUMEN` does not expose arbitrary Home Assistant services to the model.
- Model calls now go directly from `LUMEN` to `Ollama`, so no `Open WebUI`-specific `chat_id` patch is needed.
