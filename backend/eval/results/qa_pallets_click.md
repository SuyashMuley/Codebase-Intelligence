# Question answering retrieval: pallets/click

- Commit `08b803cf32`; embedding model `@cf/baai/bge-small-en-v1.5`.
- Closest-chunk cosine similarity. Answerable questions: min 0.693, median 0.783, max 0.868. Unanswerable: min 0.473, median 0.629, max 0.69.

## Decision at similarity threshold 0.75

- Answers 90% of the answerable questions and declines 100% of the unanswerable ones.
  - off_topic: declined 100% of 6 (highest similarity 0.69, named-symbol matches 0)
  - absent_feature: declined 100% of 6 (highest similarity 0.683, named-symbol matches 0)
  - absent_name: declined 100% of 3 (highest similarity 0.672, named-symbol matches 0)

## Is the labelled code in what the model would see?

- In the top retrieved chunks: 80%
- In the assembled excerpts without graph expansion: 75%
- With graph expansion: 75% (0 questions gained it, 0 lost it)
- Mean excerpts: 8.0 without, 8.0 with; mean characters: 8647 without, 8647 with.

## Labelled code in context, by number of excerpts shown

| excerpts | search only | neighbours after hits | gained | lost | neighbours replace last 2 hits |
| --- | --- | --- | --- | --- | --- |
| 3 | 70% | 70% | 0 | 0 | 50% |
| 4 | 75% | 75% | 0 | 0 | 60% |
| 5 | 75% | 75% | 0 | 0 | 70% |
| 6 | 75% | 75% | 0 | 0 | 75% |
| 8 | 75% | 75% | 0 | 0 | 75% |

## Threshold sweep (the full rule: a named symbol OR similarity)

| threshold | answers answerable | declines unanswerable | balanced | similarity only: answers / declines |
| --- | --- | --- | --- | --- |
| 0.3 | 100% | 0% | 0.500 | 100% / 0% |
| 0.325 | 100% | 0% | 0.500 | 100% / 0% |
| 0.35 | 100% | 0% | 0.500 | 100% / 0% |
| 0.375 | 100% | 0% | 0.500 | 100% / 0% |
| 0.4 | 100% | 0% | 0.500 | 100% / 0% |
| 0.425 | 100% | 0% | 0.500 | 100% / 0% |
| 0.45 | 100% | 0% | 0.500 | 100% / 0% |
| 0.475 | 100% | 7% | 0.533 | 100% / 7% |
| 0.5 | 100% | 13% | 0.567 | 100% / 13% |
| 0.525 | 100% | 13% | 0.567 | 100% / 13% |
| 0.55 | 100% | 13% | 0.567 | 100% / 13% |
| 0.575 | 100% | 20% | 0.600 | 100% / 20% |
| 0.6 | 100% | 33% | 0.667 | 100% / 33% |
| 0.625 | 100% | 47% | 0.733 | 100% / 47% |
| 0.65 | 100% | 73% | 0.867 | 100% / 73% |
| 0.675 | 100% | 87% | 0.933 | 100% / 87% |
| 0.7 | 95% | 100% | 0.975 | 95% / 100% |
| 0.725 | 95% | 100% | 0.975 | 95% / 100% |
| 0.75 | 90% | 100% | 0.950 | 85% / 100% |
| 0.775 | 80% | 100% | 0.900 | 65% / 100% |
| 0.8 | 60% | 100% | 0.800 | 40% / 100% |
| 0.825 | 50% | 100% | 0.750 | 15% / 100% |
| 0.85 | 45% | 100% | 0.725 | 5% / 100% |
| 0.875 | 45% | 100% | 0.725 | 0% / 100% |
| 0.9 | 45% | 100% | 0.725 | 0% / 100% |

Small samples: read differences between neighbouring thresholds as noise.
