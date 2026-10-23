---
name: contribute-logic
description: Turn a person's calculation — an equation, a threshold rule, an index, a scoring or allometric formula — into a platform METHOD that the gate checks and every domain pack then computes and cites. Use when someone wants to add, store or reuse logic on servirplatform ("add my formula", "count the units above 70%", "use this allometric equation", "make this calculation part of the land-cover pack").
---

# Contribute logic as a method

A **method** is logic contributed like data. It is a declarative recipe, never code:
the inputs it reads (feeds and tables the platform already serves), ordered **steps**
in a small expression language, the **outputs** with their units, the **source** of the
logic, and the contributor's own **worked examples**. The gate re-runs every worked
example and declines on a mismatch. Once approved, every answer of the method's pack
computes it for the area asked and cites the result with its steps and its
contributor, so the figure is the platform's arithmetic and the receipt covers it.

## Rules you never break

- **Their words, not yours.** The title, description, source, licence and every
  worked example come from the person. Ask for each one; never compose or guess it.
  "I don't know the licence" is an answer to record, not a gap to fill.
- **Read it back before you submit.** Say the steps in plain language ("count the
  units whose restorable share is above 70 percent, then add up their restorable
  hectares") and get a clear yes.
- **No code.** Only the expression vocabulary below exists. If the logic needs
  something it cannot say, tell the person what is missing instead of approximating.
- **Never do the arithmetic yourself in the answer.** The point of a method is that
  the platform computes it.

## Steps

1. **Ask for the logic in their own words.** What does it compute, from what, and
   for whom? Where does it come from (paper, DOI, equation or table number)? Does it
   hold everywhere, or only in some countries?
2. **Find the inputs on the platform.** Call `platform_capabilities` (its `feeds`
   list includes contributed tables and feeds) and `feeds_query(dataset=...)` to see
   a dataset's fields and a few rows. A method can read only data the platform
   already serves; if the data is not there, contribute it first as `kind=table` or
   `kind=feed`, then come back. Rasters and class areas are not method inputs yet.
3. **Draft the steps** with the person: one small step per idea, each with a name.
   A field is `<input>.<field>`; use `field(<input>, 'Exact Name')` for a column
   whose name has spaces or capitals. Put constants (a threshold, an equation's
   coefficients) in `params`, each with a unit. Name the steps that are results in
   `outputs`, each one number for the area, with its unit. Read it all back.
4. **Get a worked example with known numbers.** A few input rows and the result the
   person expects, from a case they already know: a published table, a hand
   calculation. The numbers must be theirs. Also ask for the citation and licence.
5. **Submit**: `contribute_submit(kind="method", manifest={...})`.
6. **Read the verdict and fix it together.**
   - `declined`: every problem is named. Fix them with the person, never by inventing
     a value. An example that "came out X, the example expects Y" means the steps or
     the example are wrong: check both with them.
   - `approved` or `staged`: tell them to test it by asking a question in that pack
     for a place its inputs cover. The answer cites the method's result, and a
     receipt covers it once it is published.
7. **Reuse and change.** An approved method runs in every answer of its pack whose
   area its inputs cover, for everyone. To change one, withdraw it
   (`contribute_status(contribution_id=..., action="withdraw")`) and submit the new
   version. Receipts already built on the old one stay replayable.

## References

- `references/vocabulary.md` — every operator and function a step can use.
- `references/examples.md` — two complete manifests: a threshold count and total, and
  an allometric equation over plot measurements.
- `references/declines.md` — what the gate says when it declines, and what to do.
