# Retrieval benchmark: pallets/click

- Commit `08b803cf32`; 20 questions (6 identifier, 8 concept, 6 mixed); 1214 chunks.
- Embedding model: `cloudflare:@cf/baai/bge-small-en-v1.5:cls`. Run at 2026-11-17T19:12:22+00:00.
- Indexing took 19.9 s and used 1083.4 neurons.
- A result counts as relevant when it overlaps a labelled symbol. Top 10 per question.

| Method | Recall@1 | Recall@5 | Recall@10 | MRR | MRR 95% CI | median ms |
| --- | --- | --- | --- | --- | --- | --- |
| keyword | 0.35 | 0.60 | 0.75 | 0.462 | 0.28 to 0.65 | 14.9 |
| keyword (text and path only) | 0.10 | 0.30 | 0.55 | 0.213 | 0.09 to 0.37 | 15.2 |
| vector | 0.50 | 0.65 | 0.75 | 0.571 | 0.38 to 0.78 | 214.4 |
| hybrid | 0.35 | 0.65 | 0.85 | 0.505 | 0.34 to 0.68 | 270.1 |
| hybrid (text and path only) | 0.20 | 0.45 | 0.80 | 0.348 | 0.19 to 0.52 | 258.2 |
| hybrid (vector weight 1.5) | 0.30 | 0.65 | 0.85 | 0.478 | 0.32 to 0.65 | 262.2 |
| hybrid (vector weight 2) | 0.30 | 0.60 | 0.80 | 0.472 | 0.31 to 0.65 | 246.9 |
| hybrid (vector weight 3) | 0.35 | 0.60 | 0.85 | 0.502 | 0.33 to 0.69 | 251.7 |

By question type (Recall@5 / MRR):

| Method | identifier | concept | mixed |
| --- | --- | --- | --- |
| keyword | 1.00 / 0.70 | 0.25 / 0.12 | 0.67 / 0.68 |
| keyword (text and path only) | 0.33 / 0.17 | 0.25 / 0.12 | 0.33 / 0.39 |
| vector | 0.83 / 0.72 | 0.50 / 0.43 | 0.67 / 0.61 |
| hybrid | 1.00 / 0.68 | 0.38 / 0.28 | 0.67 / 0.63 |
| hybrid (text and path only) | 0.33 / 0.39 | 0.38 / 0.28 | 0.67 / 0.39 |
| hybrid (vector weight 1.5) | 1.00 / 0.76 | 0.38 / 0.28 | 0.67 / 0.46 |
| hybrid (vector weight 2) | 0.83 / 0.75 | 0.38 / 0.27 | 0.67 / 0.46 |
| hybrid (vector weight 3) | 0.83 / 0.74 | 0.38 / 0.35 | 0.67 / 0.46 |

Question by question (the first method ranks the answer higher / lower / equal):

- keyword vs keyword (text and path only): 8 better, 0 worse, 12 tied
- hybrid vs keyword: 7 better, 4 worse, 9 tied
- hybrid vs vector: 5 better, 5 worse, 10 tied
- vector vs keyword: 9 better, 5 worse, 6 tied

With this many questions the confidence intervals are wide. Treat differences smaller than the interval as unproven, and the dataset as one repository's questions, not a general result.
