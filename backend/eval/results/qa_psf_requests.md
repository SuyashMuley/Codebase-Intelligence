# Question answering retrieval: psf/requests

- Commit `3c7baaa05a`; embedding model `@cf/baai/bge-small-en-v1.5`.
- Closest-chunk cosine similarity. Answerable questions: min 0.716, median 0.791, max 0.857. Unanswerable: min 0.453, median 0.636, max 0.734.

## Decision at similarity threshold 0.75

- Answers 92% of the answerable questions and declines 100% of the unanswerable ones.
  - off_topic: declined 100% of 6 (highest similarity 0.66, named-symbol matches 0)
  - absent_feature: declined 100% of 6 (highest similarity 0.734, named-symbol matches 0)
  - absent_name: declined 100% of 3 (highest similarity 0.706, named-symbol matches 0)

## Is the labelled code in what the model would see?

- In the top retrieved chunks: 98%
- In the assembled excerpts without graph expansion: 98%
- With graph expansion: 98% (0 questions gained it, 0 lost it)
- Mean excerpts: 8.0 without, 8.0 with; mean characters: 7160 without, 7160 with.

## Labelled code in context, by number of excerpts shown

| excerpts | search only | neighbours after hits | gained | lost | neighbours replace last 2 hits |
| --- | --- | --- | --- | --- | --- |
| 3 | 92% | 92% | 0 | 0 | 75% |
| 4 | 95% | 95% | 0 | 0 | 85% |
| 5 | 95% | 95% | 0 | 0 | 92% |
| 6 | 98% | 98% | 0 | 0 | 95% |
| 8 | 98% | 98% | 0 | 0 | 98% |

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
| 0.475 | 100% | 13% | 0.567 | 100% / 13% |
| 0.5 | 100% | 13% | 0.567 | 100% / 13% |
| 0.525 | 100% | 20% | 0.600 | 100% / 20% |
| 0.55 | 100% | 27% | 0.633 | 100% / 27% |
| 0.575 | 100% | 27% | 0.633 | 100% / 27% |
| 0.6 | 100% | 33% | 0.667 | 100% / 33% |
| 0.625 | 100% | 47% | 0.733 | 100% / 47% |
| 0.65 | 100% | 53% | 0.767 | 100% / 53% |
| 0.675 | 100% | 73% | 0.867 | 100% / 73% |
| 0.7 | 100% | 80% | 0.900 | 100% / 80% |
| 0.725 | 98% | 93% | 0.954 | 98% / 93% |
| 0.75 | 92% | 100% | 0.963 | 92% / 100% |
| 0.775 | 70% | 100% | 0.850 | 70% / 100% |
| 0.8 | 48% | 100% | 0.738 | 35% / 100% |
| 0.825 | 48% | 100% | 0.738 | 20% / 100% |
| 0.85 | 42% | 100% | 0.713 | 8% / 100% |
| 0.875 | 40% | 100% | 0.700 | 0% / 100% |
| 0.9 | 40% | 100% | 0.700 | 0% / 100% |

Small samples: read differences between neighbouring thresholds as noise.
