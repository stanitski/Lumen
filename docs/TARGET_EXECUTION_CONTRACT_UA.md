# TARGET_EXECUTION_CONTRACT_UA.md

## Статус

Це historical target-state document для старого planner/execution runtime.

Він не описує поточну версію Lumen і не повинен використовуватися як source of truth для current architecture.

## Поточний source of truth

Для поточної версії Lumen орієнтуйся на:

- `README.md`
- `PROJECT_MAP.md`
- `docs/CHAT_ONLY_RUNTIME_CONTRACT_UA.md`
- `docs/LUMEN_ARCHITECTURE_GUIDE_UA.md`
- live code в `src/lumen/main.py` і `src/lumen/services/chat_only.py`

## Чому документ збережено

Документ залишено лише як архівне design reference для старого `Plan -> Policy -> Execute -> Persist -> Deliver` напряму.

Він може бути корисним тільки якщо колись окремо проєктується нова optional action runtime, але не як опис сьогоднішньої системи.
