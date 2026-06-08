<!-- Honor-system testing checklist (see CLAUDE.md → "Testing — Definition of Done").
     CI reports results per domain but does not block merge — please self-certify. -->

## What & why


## Testing — Definition of Done
- [ ] Added/extended tests for this change (new feature → new tests; bug fix → a test that fails before, passes after), in the right tier.
- [ ] `./jt` is green for the domain(s) touched (`./jt back` / `./jt front`), and `./jt e2e` if a user-facing flow / API contract changed.
- [ ] Coverage didn't regress; floors not lowered. Ratcheted a floor up if real coverage now allows it.
- [ ] No real network/LLM/secrets in tests; mocked at the module seam.

## Notes for reviewers

