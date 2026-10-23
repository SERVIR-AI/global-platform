# Expression vocabulary

| Kind | What exists |
|---|---|
| Arithmetic | `+ - * / ** %`, unary `-` |
| Comparisons | `> >= < <= == !=`, `x in [a, b]`, `x not in [...]` (text: only `==`, `!=`, `in`) |
| Logic | `and`, `or`, `not`, `a if condition else b` |
| Per-row functions | `log` (natural), `log10`, `exp`, `sqrt`, `abs`, `round(x, digits)`, `clip(x, lo, hi)`, `where(cond, a, b)`, `minimum(a, b)`, `maximum(a, b)` |
| Aggregates (one number) | `count(<input>)`, `count(<condition>)`, `sum(x, where=cond)`, `mean(...)`, `min(...)`, `max(...)`, `median(...)`, `weighted_mean(x, w, where=cond)` |
| Fields | `<input>.<field>`, `field(<input>, 'Exact Name')` |

Empty values are skipped by aggregates. Rows of two different inputs never line up:
reduce each input to a number with an aggregate before combining them. A step can
use the parameters and any earlier step.
