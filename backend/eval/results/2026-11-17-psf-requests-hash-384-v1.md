# Retrieval benchmark: psf/requests

- Commit `3c7baaa05a`; 40 questions (12 identifier, 16 concept, 12 mixed); 523 chunks.
- Embedding model: `hash-384-v1`. Run at 2026-11-17T19:05:27+00:00.
- Indexing took 1.3 s.
- A result counts as relevant when it overlaps a labelled symbol. Top 10 per question.

| Method | Recall@1 | Recall@5 | Recall@10 | MRR | MRR 95% CI | median ms |
| --- | --- | --- | --- | --- | --- | --- |
| keyword | 0.45 | 0.75 | 0.78 | 0.577 | 0.45 to 0.71 | 6.7 |
| keyword (text and path only) | 0.25 | 0.68 | 0.72 | 0.412 | 0.30 to 0.54 | 6.7 |
| vector | 0.30 | 0.62 | 0.72 | 0.438 | 0.31 to 0.57 | 2.2 |
| hybrid | 0.40 | 0.72 | 0.80 | 0.531 | 0.40 to 0.66 | 8.2 |
| hybrid (text and path only) | 0.35 | 0.70 | 0.80 | 0.486 | 0.36 to 0.61 | 8.2 |

By question type (Recall@5 / MRR):

| Method | identifier | concept | mixed |
| --- | --- | --- | --- |
| keyword | 1.00 / 0.92 | 0.56 / 0.30 | 0.75 / 0.61 |
| keyword (text and path only) | 0.83 / 0.56 | 0.56 / 0.30 | 0.67 / 0.42 |
| vector | 0.58 / 0.44 | 0.44 / 0.30 | 0.92 / 0.62 |
| hybrid | 0.75 / 0.67 | 0.62 / 0.40 | 0.83 / 0.57 |
| hybrid (text and path only) | 0.75 / 0.61 | 0.62 / 0.40 | 0.75 / 0.48 |

Question by question (the first method ranks the answer higher / lower / equal):

- keyword vs keyword (text and path only): 10 better, 0 worse, 30 tied
- hybrid vs keyword: 9 better, 12 worse, 19 tied
- hybrid vs vector: 18 better, 6 worse, 16 tied
- vector vs keyword: 7 better, 21 worse, 12 tied

With this many questions the confidence intervals are wide. Treat differences smaller than the interval as unproven, and the dataset as one repository's questions, not a general result.
