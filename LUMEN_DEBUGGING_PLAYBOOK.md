# Lumen Debugging Playbook

Практичний playbook для діагностики:

- wrong answers
- wrong skill selection
- memory confusion

TBD:
`LUMEN_CODE_GUIDE.md` у поточному repo не знайдено. Цей документ базується на live code та `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md`.

## 1. Як відтворити bad answer

1. Зафіксувати точний `user_text`.
2. Зафіксувати ingress: `/chat`, `/connector`, Home Assistant voice, або `/api/chat`.
3. Зафіксувати `user_id`, час, і чи був `deliver=True`.
4. Повторити той самий запит двічі:
   - один раз як plain text
   - один раз через той самий реальний ingress
5. Порівняти:
   - selected intent/skill
   - final answer
   - чи був fallback текст

Мінімальний safe trace order:

1. `src/lumen/services/chat.py`
2. `src/lumen/skills/planner.py`
3. `src/lumen/tasks/executor.py`
4. `src/lumen/services/memory.py`

## 2. Які логи дивитись першими

Шукати спочатку такі існуючі логи:

- `Planner context for ...` у `MemoryService.build_planner_context`
- `Planner intent classified ...` у `SkillPlanner.plan`
- `Planner candidate matches before intent filter ...`
- `Planner candidate matches after intent filter ...`
- `Skill planner response (parsed)` або `Skill planner response (unparsed)`
- `Planner final selection ...`
- `Skill planner finished for request ...`
- `Skill execution finished for request ...`
- `Skill chat request ... finished ...`
- `Memory extraction failed after delivery ...`

Якщо відповідь виглядає як generic ack, окремо перевірити fallback рядки в `ChatService._primary_answer()` і `ChatService.process_chat_request_result()`.

## 3. Як відрізнити тип проблеми

### Bad intent classification

- Симптоми:
  `action_intent` не відповідає запиту; звичайний chat іде в action path або навпаки.
- Файли/функції:
  `src/lumen/skills/planner.py`
  `SkillPlanner.classify_action_intent()`
  `SkillPlanner.plan()`
- Exact debug log to add:
```python
self._logger.debug(
    "Intent debug: user=%s text=%r recent_turns=%s action_default=%s classified=%s",
    user_id,
    user_text,
    len(recent_turns),
    action_default,
    action_intent,
)
```
- Likely fix direction:
  звузити prompt для intent classification, прибрати misleading recent context, або скоригувати `intents`/default intent.

### Wrong skill selected

- Симптоми:
  `action_intent` правильний, але selected skill не той; або planner падає в `general_response` при явній дії.
- Файли/функції:
  `src/lumen/skills/planner.py`
  `SkillPlanner.plan()`
  `SkillPlanner._filter_matches_by_action_intent()`
  `SkillPlanner._prefer_explicit_action_match()`
- Exact debug log to add:
```python
self._logger.debug(
    "Skill routing debug: intent=%s allowed=%s selected_raw=%s selected_final=%s",
    effective_action_intent,
    [match.skill.skill_id for match in matches],
    raw.get("selected_skills"),
    self._format_selected_for_log(selected),
)
```
- Likely fix direction:
  підправити intent filter, registry matching reasons, або planner prompt про `general_response`.

### Noisy memory retrieval

- Симптоми:
  відповідь тягне старий або чужий контекст; follow-up topic перекриває поточний запит.
- Файли/функції:
  `src/lumen/services/memory.py`
  `MemoryService.build_planner_context()`
  `MemoryService.classify_turn_topics()`
  `MemoryService.classify_turn_retention()`
- Exact debug log to add:
```python
self._logger.debug(
    "Memory retrieval debug: user=%s mode=%s facts=%s user_turns=%s topic_turns=%s knowledge=%s payload=%r",
    user_id,
    context_mode,
    len(user_facts),
    len(user_turns),
    len(user_topic_turns),
    len(user_knowledge),
    payload_text,
)
```
- Likely fix direction:
  зменшити topic expansion, tighten low-signal follow-up rules, або відфільтрувати operational turns з retrieval.

### Bad prompt assembly

- Симптоми:
  intent і skill наче правильні, але LLM відповідає так, ніби не бачить частину контексту або бачить його в неправильному порядку.
- Файли/функції:
  `src/lumen/skills/planner.py`
  `SkillPlanner._build_messages()`
  `src/lumen/services/chat.py`
  `ChatService.process_chat_request_result()`
- Exact debug log to add:
```python
self._logger.debug(
    "Planner prompt debug: user=%s matches=%s recent_turns=%s last_user_text=%r",
    user_id,
    [match.skill.skill_id for match in matches],
    len(recent_turns),
    user_text,
)
```
- Likely fix direction:
  перевірити порядок system/user messages, clarification merge, і чи не потрапляє `supplemental_context` туди, де не треба.

### Model hallucination

- Симптоми:
  route і skill правильні, tool data валідні, але фінальна natural-language відповідь вигадує деталі або суперечить execution result.
- Файли/функції:
  `src/lumen/tasks/executor.py`
  `ActionExecutor._step_respond()`
  `ActionExecutor._step_llm_call()`
  `ActionExecutor._run_llm_call()`
- Exact debug log to add:
```python
self._logger.debug(
    "LLM answer debug: step=%s skill=%s prompt_chars=%s response_chars=%s",
    step_id,
    skill.skill_id,
    len(rendered_prompt_text),
    len(content),
)
```
- Likely fix direction:
  зменшити freedom у prompt, дати структуровані tool outputs у prompt, або замінити generation на template/rendered response.

### Tool failure

- Симптоми:
  planner обрав правильний skill, але step падає, частково виконується, або повертає пусті/помилкові data.
- Файли/функції:
  `src/lumen/tasks/executor.py`
  `ActionExecutor.execute_skill()`
  `ActionExecutor._execute_step()`
  `ActionExecutor._step_call_service()`
  `ActionExecutor._step_create_reminder()`
  `ActionExecutor._step_skill()`
- Exact debug log to add:
```python
self._logger.debug(
    "Step debug: skill=%s step=%s type=%s rendered_step=%s",
    skill.skill_id,
    step_id,
    step_type,
    step,
)
```
- Likely fix direction:
  перевірити rendered variables, step contract, integration payload, або short-circuit conditions у конкретному step type.

### Fallback failure

- Симптоми:
  користувач бачить generic ack на кшталт `Запит оброблено.` або пусту/надто загальну відповідь після формально успішного flow.
- Файли/функції:
  `src/lumen/services/chat.py`
  `ChatService._primary_answer()`
  `ChatService.process_chat_request_result()`
- Exact debug log to add:
```python
self._logger.warning(
    "Fallback answer used: request=%s intent=%s skills=%s outcome=%s delivered=%s responses=%s",
    request_id,
    plan.action_intent,
    [item.skill_id for item in plan.selected_skills],
    execution.outcome,
    execution.delivered,
    [result.responses for result in results],
)
```
- Likely fix direction:
  не маскувати empty responses generic ack-ом, піднімати явну помилку для blank dialogue result, або краще збирати `primary_answer`.

## 4. 5 example debug scenarios

### Home Assistant action misunderstood

- Перевірити `SkillPlanner.classify_action_intent()` і `SkillPlanner.plan()`.
- Якщо skill правильний, але дія не та: дивитися `ActionExecutor.resume_pending_interaction()` і `ActionExecutor._step_call_service()`.
- Перший debug log:
```python
self._logger.debug("HA action debug: text=%r action_intent=%s selected=%s", user_text, action_intent, selected)
```

### Memory answer uses outdated info

- Перевірити `MemoryService.build_planner_context()` і чи topic expansion підтягнув старі `dialogue_turn` embeddings.
- Перший debug log:
```python
self._logger.debug("Outdated memory debug: payload=%r topic_hits=%s summary=%s", payload_text, len(user_topic_turns), bool(user_topic_summary))
```

### Reminder/task command goes to wrong path

- Перевірити `SkillPlanner.classify_action_intent()`, `ChatService._apply_deterministic_plan_guards()`, `ActionExecutor._step_create_reminder()`.
- Перший debug log:
```python
self._logger.debug("Reminder routing debug: text=%r intent=%s selected=%s", user_text, plan.action_intent, [item.skill_id for item in plan.selected_skills])
```

### General chat incorrectly triggers a skill

- Перевірити, чи `action_intent` не став non-`none`, і чи `matches_after_filter` не прибрали conversational fallback.
- Перший debug log:
```python
self._logger.debug("General chat misroute: text=%r classified=%s matches=%s", user_text, intents, self._format_matches_for_log(matches_after_filter))
```

### Skill execution succeeds but final answer is wrong

- Перевірити `ActionExecutor.execute_skill()` vs `ChatService._primary_answer()`.
- Якщо steps `ok=True`, але answer поганий, проблема часто в `respond` prompt або final aggregation.
- Перший debug log:
```python
self._logger.debug("Final answer mismatch: skill_results=%s primary_answer=%r", [(r.skill_id, r.ok, r.responses) for r in results], primary_answer)
```

## 5. Швидкий decision tree

1. Є `action_intent`, який не схожий на запит:
   проблема в classification.
2. `action_intent` ок, але selected skill дивний:
   проблема в planner/registry routing.
3. Selected skill ок, але data з минулого перекривають поточне:
   проблема в memory retrieval.
4. Skill і data ок, але generated text дивний:
   проблема в prompt assembly або hallucination.
5. Step впав або повернув пустий output:
   проблема в tool failure.
6. Все пройшло, але користувач бачить generic ack:
   проблема в fallback failure.

## 6. Safe first chats for Codex

- `Розбери wrong-answer bug у Lumen. Почни з src/lumen/services/chat.py, src/lumen/skills/planner.py, src/lumen/tasks/executor.py. Не міняй schema/config. Поверни root-cause hypothesis, smallest safe fix set, і minimal verification plan.`
- `Розбери memory confusion у Lumen. Почни з src/lumen/services/memory.py і src/lumen/services/chat.py. Перевір build_planner_context, topic expansion, retention/meta threading. Код поки не міняй.`
- `Розбери planner misrouting у Lumen. Почни з src/lumen/skills/planner.py. Поясни action_intent, candidate matches, selected_skills, і де саме з’являється wrong path.`
