# Demo script (about 8 minutes)

Use `pallets/click`: it is small (about 1,200 chunks), well known, and the questions below were checked against it. Index it before the demo; it takes a few minutes and about 1,000 of the 10,000 daily embedding units. The Ask steps need the OpenRouter free model to be answering: if one fails, retry it once and say so; that is the real behaviour.

## 1. Sign in and connect (1 min)
- Open the app, **Sign in with GitHub**.
- Paste `https://github.com/pallets/click`, **Connect**, open it, **Index latest commit**. Keep the tab open: the browser drives the job step by step.
- Say: there is no background worker on the free tier; the job is a resumable state machine. Closing the tab pauses it and reopening resumes.

## 2. Overview and Files (1 min)
- Watch fetch, parse, embed and graph complete; point at the counts.
- **Files**: open `src/click/decorators.py`. Mention that secret-looking values are redacted before storage.

## 3. Search (1 min)
- Search `how are options validated`. Say: it matches meaning, not just words; click a result to jump to its lines.
- Search `make_pass_decorator` to show an exact-name lookup.

## 4. Graph and Impact (2 min)
- **Graph**, use the file finder to open `src/click/core.py`. Say: solid lines are proven by the code; dashed ones are unresolved and labelled with a reason. The app counts them instead of hiding them.
- Select a symbol and read the **Impact** panel: dependents with the path to each, suggested tests with their basis.
- Optional: **AI review** (uses one model request). Say it is kept separate from the proven results and can only cite files the analysis found.

## 5. Ask (3 min)
Ask these in order:

1. `Where is make_pass_decorator defined and what does it do?` Expect numbered citations. **Click a citation**: the Files tab opens at the cited lines. Say: each statement had to quote the code exactly or it would have been dropped.
2. `What is this repo about?` Expect an overview that cites the README.
3. `What does MigrationRunner do?` (a name that does not exist). Expect "I cannot determine this from the code." Say: the model is allowed to decline, and nothing it says is shown unless it quotes the code.
4. Optional: `What is the capital of France?` Same refusal.

## 6. Clean up (30 s)
- **Disconnect** the repository to show that its data is removed.

## If something goes wrong
- "Waking up the server" on the first load: the free API was asleep; wait up to a minute.
- Ask says the model took too long or is busy: free models do that; retry. Failed attempts are not counted against the 25 a day.
- Embedding quota reached: indexing resumes the next day; use an already indexed repository.
