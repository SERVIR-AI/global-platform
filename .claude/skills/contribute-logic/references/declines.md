# Common declines

| The gate says | What to do |
|---|---|
| missing required field 'source' (or title, description, license, examples) | Ask the person. Never write it for them. |
| input names a dataset the platform does not serve | Find the right name with `platform_capabilities`; or contribute the data first. |
| input has no field … did you mean … | Use the field name exactly as `feeds_query` shows it. |
| mixes the rows of two inputs | Aggregate each input to a number first, then combine the numbers. |
| an output is one value per row | Wrap it in `sum()`, `mean()`, `count()` or another aggregate. |
| example N came out X, the example expects Y | Check the steps and the example with the person. Do not change the expected number to make it pass. |
| a method named … is already on the platform | Choose a new name, or withdraw the old one first. |
