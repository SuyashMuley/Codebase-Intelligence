# Retrieval benchmark: psf/requests

- Commit `3c7baaa05a`; 40 questions (12 identifier, 16 concept, 12 mixed); 523 chunks.
- Embedding model: `cloudflare:@cf/baai/bge-small-en-v1.5:cls`. Run at 2026-11-17T19:11:19+00:00.
- Indexing took 6.9 s and used 454.71 neurons.
- A result counts as relevant when it overlaps a labelled symbol. Top 10 per question.

| Method | Recall@1 | Recall@5 | Recall@10 | MRR | MRR 95% CI | median ms |
| --- | --- | --- | --- | --- | --- | --- |
| keyword | 0.45 | 0.75 | 0.78 | 0.577 | 0.45 to 0.71 | 8.7 |
| keyword (text and path only) | 0.25 | 0.68 | 0.72 | 0.412 | 0.30 to 0.54 | 8.2 |
| vector | 0.75 | 0.95 | 0.97 | 0.826 | 0.73 to 0.92 | 194.0 |
| hybrid | 0.62 | 0.85 | 0.90 | 0.721 | 0.60 to 0.84 | 192.8 |
| hybrid (text and path only) | 0.55 | 0.80 | 0.88 | 0.662 | 0.54 to 0.79 | 195.4 |
| hybrid (vector weight 1.5) | 0.65 | 0.90 | 0.90 | 0.745 | 0.63 to 0.85 | 182.5 |
| hybrid (vector weight 2) | 0.65 | 0.90 | 0.90 | 0.749 | 0.63 to 0.86 | 146.6 |
| hybrid (vector weight 3) | 0.65 | 0.90 | 0.90 | 0.753 | 0.64 to 0.86 | 194.9 |

By question type (Recall@5 / MRR):

| Method | identifier | concept | mixed |
| --- | --- | --- | --- |
| keyword | 1.00 / 0.92 | 0.56 / 0.30 | 0.75 / 0.61 |
| keyword (text and path only) | 0.83 / 0.56 | 0.56 / 0.30 | 0.67 / 0.42 |
| vector | 1.00 / 0.96 | 0.88 / 0.78 | 1.00 / 0.76 |
| hybrid | 1.00 / 0.96 | 0.75 / 0.59 | 0.83 / 0.65 |
| hybrid (text and path only) | 0.92 / 0.79 | 0.75 / 0.59 | 0.75 / 0.62 |
| hybrid (vector weight 1.5) | 1.00 / 0.96 | 0.81 / 0.65 | 0.92 / 0.66 |
| hybrid (vector weight 2) | 1.00 / 0.96 | 0.81 / 0.66 | 0.92 / 0.66 |
| hybrid (vector weight 3) | 1.00 / 0.96 | 0.81 / 0.65 | 0.92 / 0.69 |

Question by question (the first method ranks the answer higher / lower / equal):

- keyword vs keyword (text and path only): 10 better, 0 worse, 30 tied
- hybrid vs keyword: 17 better, 2 worse, 21 tied
- hybrid vs vector: 4 better, 11 worse, 25 tied
- vector vs keyword: 19 better, 3 worse, 18 tied

With this many questions the confidence intervals are wide. Treat differences smaller than the interval as unproven, and the dataset as one repository's questions, not a general result.
