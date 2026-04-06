# VD-017: Material Outline — structured lossless outline before agents

**Фаза:** 7 — Pre-Agent Processing
**Пріоритет:** Високий
**Залежності:** VD-011 (E2E pipeline done)

---

## Проблема

SourceDocument після ingestion містить:
- **STT:** raw транскрипція зі словами-паразитами, повторами, заїканнями
- **VD:** per-scene summaries з повторними описами одного й того ж IDE/layout

Всі downstream агенти (ArchitectAgent, майбутні test/exercise generators) отримують цей raw вхід і самостійно борються з шумом. Якість їхньої роботи залежить від якості вхідних даних.

## Рішення

Новий етап pipeline між ingestion та агентами: **MaterialOutline** — один LLM виклик, який перетворює SourceDocument на **"ідеальний конспект"**.

```
Ingestion → SourceDocument (raw, зберігається як є)
               ↓ LLM
           MaterialOutline (структурований, зберігається окремо)
               ↓
           ArchitectAgent / інші агенти (працюють з outline)
```

### Принцип: lossless restructuring, NOT summarization

MaterialOutline — це **структуроване переформатування**, а не стиснення:
- **Прибирає:** механічні артефакти (повтори слів, "еее", дублі VD описів одного екрану, заїкання STT)
- **Зберігає:** кожну тезу, кожен приклад, кожне пояснення лектора, кожен фрагмент коду
- **Організує:** по тематичних блоках з чіткими межами, key_concepts, часовими мітками
- **Навігація:** кожна секція вказує на time range в raw SourceDocument для drill-down

Аналогія: студент зробив **ідеальний конспект лекції** — структурований, з виділенням головних ідей, з описом кожного блоку. Нічого не загублено, але все організовано для зручної роботи.

Спеціалізація і "втрата" частини інформації — задача downstream агентів (тести, завдання, методичні рекомендації). Outline зберігає універсальність.

### Drill-down capability

```
Питання: "що було про map()?"
    ↓
MaterialOutline.sections → пошук по key_concepts / narration
    ↓
OutlineSection(start_sec=340, end_sec=480, title="Built-in map()")
  narration: повний опис що говорив лектор (очищений від шуму)
  screen_content: що було на екрані
  code_snippets: [exact code]
    ↓
Достатньо? → OK
Потрібно точніше? → raw SourceDocument chunks за start_sec=340..480
    ↓
Дослівна STT транскрипція + VD фрейми за цей інтервал
```

## Структура даних

### MaterialOutline (Pydantic model)

```python
class PresenterInfo(BaseModel):
    description: str        # хто лектор (роль, бекграунд якщо згадано)
    style: str              # розмовний / академічний / практичний
    delivery_notes: str     # особливості подачі (темп, відступи, гумор)

class CodeSnippet(BaseModel):
    language: str           # мова програмування
    code: str               # код as-is, без перефразування
    context: str            # що демонструє (1 речення)

class OutlineSection(BaseModel):
    start_sec: float
    end_sec: float
    title: str              # тематична назва блоку (1 рядок)
    narration: str          # що говорить лектор — очищено від артефактів,
                            # але кожна теза/приклад/пояснення збережено
    screen_content: str     # що на екрані — кожна змістовна зміна описана,
                            # без повторів boilerplate ("same IDE layout")
    code_snippets: list[CodeSnippet]  # код exactly as shown
    key_concepts: list[str]           # концепти що вводяться/пояснюються

class MaterialOutline(BaseModel):
    # Макроінформація
    title: str              # назва/тема матеріалу
    duration_sec: float
    language: str            # мова викладання
    source_type: str         # video / lecture / screencast / presentation

    # Лектор/автор
    presenter: PresenterInfo

    # Огляд (навігаційний, НЕ заміна секцій)
    summary: str             # 2-3 речення для швидкої орієнтації
    topics: list[str]        # ключові теми
    tools: list[str]         # IDE, мови, бібліотеки
    target_audience: str     # на кого орієнтовано
    prerequisites: list[str] # що передбачається відомим

    # Тематичні блоки (хронологічно, гранулярність 2-5 хв)
    sections: list[OutlineSection]
```

### Зберігання

Нове поле в `MaterialEntry`:
```python
outline_content: Mapped[str | None] = mapped_column(Text)
```

- `processed_content` = raw SourceDocument JSON (як зараз, не змінюється)
- `outline_content` = MaterialOutline JSON (новий)
- Агенти читають `outline_content`; якщо None — fallback на `processed_content`

Alembic міграція: `ALTER TABLE material_entries ADD COLUMN outline_content TEXT`.

## Вибір моделі

Задача — **lossless restructuring** з multilingual вхідними даними:
- Українська STT + англійська VD → структурований outline
- Технічна точність (код, імена функцій — as-is)
- Розрізнення змістовної інформації від механічних артефактів

Нова action в `external_services.yaml`:
```yaml
material_outline:
  strategy: default
  requires: [structured_output]
  chain:
    default: [gemini-2.5-flash, deepseek-chat]
    quality: [claude-sonnet, gemini-2.5-pro]
    budget: [deepseek-chat]
```

A/B тестування: запустити spike з різними strategy, порівняти повноту збереження інформації.

## Оцінка токенів

| Відео | Input | Output | Вартість (Flash) |
|-------|-------|--------|-----------------|
| 16 хв | ~8K tokens | ~5K tokens | ~$0.01 |
| 2 год | ~50K tokens | ~25K tokens | ~$0.20 |
| Курс 47 год | ~25 відео × ~50K | ~25 × ~25K | ~$5 |

Output більший ніж при summarization, бо зберігаємо повноту інформації. Один LLM виклик на матеріал.

## Кроки реалізації

### Крок 1: Pydantic schemas
- `src/course_supporter/models/outline.py` — MaterialOutline, OutlineSection, PresenterInfo, CodeSnippet
- Тести валідації

### Крок 2: Outline prompt
- `src/course_supporter/prompts/outline/v1.yaml` — system prompt для LLM
- Ключова інструкція: "ідеальний конспект", lossless restructuring, НЕ summarization
- Вхід: SourceDocument chunks (STT + VD) з часовими мітками
- Вихід: MaterialOutline JSON

### Крок 3: OutlineAgent
- `src/course_supporter/agents/outline.py` — OutlineAgent
- Приймає SourceDocument → повертає MaterialOutline
- Використовує ModelRouter (action=`material_outline`, configurable strategy)
- Structured output з Pydantic validation + retry

### Крок 4: DB migration + storage
- Alembic migration: `outline_content` поле
- `MaterialEntryRepository.save_outline(material_id, outline_json)`
- Pipeline wiring: після ingestion → outline → save

### Крок 5: Spike на реальних даних
- Взяти кешований SourceDocument з VD-011
- Один LLM виклик → MaterialOutline
- Верифікація: порівняти outline з raw — чи збережено всю інформацію
- A/B: Flash vs quality strategy

### Крок 6: Agent wiring
- ArchitectAgent (та інші агенти) читають `outline_content` замість `processed_content`
- Fallback на raw якщо outline відсутній

## Acceptance Criteria

- [ ] MaterialOutline schema з валідацією
- [ ] OutlineAgent: SourceDocument → MaterialOutline (один LLM call, lossless)
- [ ] DB migration: `outline_content` поле в `material_entries`
- [ ] Spike: outline для video1_python_16min — людина верифікує повноту збереження інформації
- [ ] A/B: порівняння Flash vs quality strategy (повнота, не стислість)
- [ ] ArchitectAgent читає outline (з fallback на raw)
- [ ] Всі існуючі тести зелені
