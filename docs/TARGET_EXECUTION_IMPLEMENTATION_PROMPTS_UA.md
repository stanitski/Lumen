# TARGET_EXECUTION_IMPLEMENTATION_PROMPTS_UA.md

## Статус

Це historical prompt set для старого planner/execution contract.

Він не відповідає поточному `chat_only` runtime Lumen і не повинен використовуватися для звичайних поточних змін у репозиторії.

## Поточний source of truth

Для актуальної версії системи використовуй:

- `README.md`
- `PROJECT_MAP.md`
- `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`
- `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md`
- live code у `src/lumen/main.py`, `src/lumen/services/chat_only.py`, `src/lumen/services/memory.py`

## Чому документ збережено

Його залишено лише як історичний матеріал про стару execution-архітектуру та старі Codex prompt flows.

Якщо в майбутньому з'явиться новий окремий action/reminder runtime, для нього треба буде написати нові prompts з нуля під новий live contract, а не відновлювати ці інструкції як є.
