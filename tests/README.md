# Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt   # once
.venv/bin/python -m pytest                                # ~3s, no network, no cost
```

Nothing here calls a provider. Each pipeline talks to the model through one
seam -- `_generate_batch` for trivia, `_ask` for puzzles -- and the fixtures in
`conftest.py` replace that seam with a scripted fake. Everything around it is
the real code: batching, dedup, refill, caching, validation, the Flask routes.

## What each file guards

| File | Guards |
|---|---|
| `test_trivia_shortfall.py` | The 14-of-15 deadlock: asking for a batch of one, resending an identical prompt (a cache hit that replays the same rejected reply), discarding partial content, and aborting the whole book at the first short chapter. |
| `test_trivia_resolve.py` | The resolve escape hatches: lowering `trivia_count`, trimming a draft to a lowered quota, and topping a short chapter back up without re-buying what it already has. |
| `test_trivia_resolve_route.py` | The resolve endpoint end to end -- the wiring that makes the button actually finish a book. |
| `test_trivia_validation.py` | Every rule in the export gate: counts, choice shape, answer spread, and the fact-count tolerance. |
| `test_trivia_status_config.py` | The status payload carries the book's own config, and the browser refuses to resolve with a form belonging to a different book. |
| `test_puzzle_provider.py` | Provider rejections are recognised as their own failure type, retried with backoff, and never cached -- a cached refusal is permanent and free, so the build could never heal. |
| `test_puzzle_degradation.py` | One unbuildable puzzle costs the book that puzzle and a warning, not the run. |

## Adding a test for a bug

Write the test first and watch it fail against the unfixed code. A test that
passes both before and after guards nothing -- several here had to be rewritten
because the first version did not actually reproduce the failure.

To check the suite still has teeth, break a fix on purpose and confirm
something goes red:

```bash
# expect failures
sed -i '' 's/need + REFILL_SURPLUS/need/' trivia/pipeline.py && .venv/bin/python -m pytest
git checkout trivia/pipeline.py
```
