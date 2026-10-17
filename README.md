# Bellhaven CRM: location review

This tool compares Bellhaven Senior Living's public community list with the CRM, proposes fixes, and writes only the fixes a person approves.

I built it around one question: could a new analyst work through the review queue as accurately as an expert would? So every proposal arrives as one card. The card shows what will change and the evidence behind it, says how the billing rule applies, and tells the reviewer what to check before approving.

## How to run

1. Python 3.12:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Set the CRM token in your shell (it is never stored in a file):
   ```powershell
   $env:BH_TOKEN = "<your bh_ token>"
   ```
3. Propose changes. This reads the website and the CRM, saves proposals to `state.db` and raw copies to `data/` (not committed), and never writes to the CRM:
   ```bash
   python pipeline.py
   ```
4. Review:
   ```bash
   streamlit run app.py
   ```
   - **To review:** one card at a time.
     - Keys: `A` or `→` opens a confirm step, and pressing it again writes. `R` or `←` rejects, with an optional reason. `S` or `↓` skips, and `U` or `↑` brings back the last skipped card.
     - Each card links to the facility's page on the website.
   - **History:** every decision, with the CRM id it touched.
   - **Look up in CRM:** reads any account live, to confirm that a write landed.
5. Tests (no network; the CRM is mocked):
   ```bash
   python test_rules.py
   ```

Files: `scrape.py` (website), `match.py` (matching and proposals), `rules.py` (thresholds and the SOP), `pipeline.py` (daily run), `crm.py` (API client), `app.py` and `review_card/` (review app), `state.db` (proposals and decisions).

## How I defined a match

I started from what proves two records are the same facility. Strongest evidence first:

1. **Same street and zip.**
2. **Same street, city and state, but a different zip.** The zip is the likely error.
3. **Same name (fuzzy score of 85 or more), city and state, but a different address.** The address is the likely error.

A name alone never matches across cities or states. Two facilities called "Amberly Manor" in Ohio and Colorado are different places, and the tool flags the look-alike rather than merging them.

Before comparing, addresses are lowercased, stripped of punctuation, and abbreviated (Street→St, North→N), so "1120 West Main Street" equals "1120 W Main St". Names ignore a leading "The" and punctuation.

Every account is searched, not only the ones already under Bellhaven. That is how facilities filed under another parent get found.

When several accounts sit at one location, I keep one as the surviving record and retire the others. The survivor is chosen in this order:
1. Not Inactive.
2. Already under Bellhaven.
3. Has billing history.
4. Phone matches the website.
5. Most complete record.

## What the tool proposes

| Card | When | What approving does |
|---|---|---|
| Add new facility | A website location with no CRM account | Creates it under Bellhaven |
| Change of ownership (CHOW): keep old account, add new one | Under another parent, with revenue and unpaid AR | Creates the facility under Bellhaven and sets `chow_current_account` on the old account. The old account is otherwise untouched |
| Link old account to new owner (CHOW) | A Bellhaven account gone from the website, with billing, and another owner's account at the same address | Sets `chow_current_account` on the old account |
| Move under Bellhaven | Under another parent, and the billing rule allows a move | Sets `parent_id`, plus any name or address fix |
| Fix name / Fix address | Under Bellhaven, but the name or address is out of date | Sets only the fields that differ |
| Retire duplicate record | A second account at the same location | Sets `duplicate_of_account` to the survivor and status to `Inactive` |
| Not on website: flag for review | Under Bellhaven, not on the website, and no new owner found | Sets status to `Needs Review` |

Accounts that already match on address, name and parent get no card. Every write adds a dated, past-tense line to the account's note, for example "Moved under Bellhaven Senior Living (was Harborview Care Group) to match the Bellhaven website." Existing notes are kept.

## The CHOW (change of ownership) SOP

The rule is in `rules.py` (`plan_reparent`). When an account needs a new parent:
- **Revenue > 0 and unpaid AR > 0:** don't move it. Create a new account under Bellhaven and point the old one at it with `chow_current_account`.
- **Otherwise:** move the existing account.

Blank money fields count as 0. The card shows the rule's verdict next to the figures, and Approve re-checks the rule against the live account before writing. If billing changed since the proposal was made, Approve refuses to move the account.

## Re-runs are safe

- **Every proposal has a key:** its type, its target, and a hash of the exact change. The pipeline only inserts keys it has never seen, so a decided item is never proposed again.
- **The matcher skips work that is already done:**
  - retired duplicates;
  - accounts already linked by a CHOW;
  - accounts already set to Needs Review.

  So even a lost `state.db` would not bring those back.
- **A create that failed partway can be retried:** before creating, the app checks for a Bellhaven account already at that location. A retry therefore never makes a second copy.
- **A broken scrape stops the run:** if any location is missing its street or zip, nothing is proposed.

## Schedule

`.github/workflows/daily.yml` runs `pipeline.py` daily at 12:00 UTC, with `BH_TOKEN` from repo secrets, and commits `state.db` back. It only proposes changes; approvals happen in the app. It is configured but not live.

## Judgment calls

- **Ashtabula (rejected):** the CRM billing street is "PO Box 517", and the website shows the building at 3156 W Prospect Rd. A PO box is a valid mailing address for billing, and overwriting it would misroute invoices. I rejected the proposal and changed the matcher so that a PO box billing street is kept, while zip and city are still checked.
- **Sandusky:** it is not on the website and has $130,000 revenue and $5,200 unpaid AR, so the SOP blocks a move. A Millstone account sits at the same address with no billing, so I linked the old account to it as the likely new owner. That is plausible but not confirmed.
- **Alliance and Coldwater:** both are gone from the website, with no billing and no other account at their address. They are set to Needs Review instead of guessed.
- **Kettering:** none of the three copies has the website phone, so the most complete record survives.
- **Amberly Manor and Union Square:** created as new facilities. The similar accounts are in another state, or at a different street with a different phone.
- **Dayton (The Arbors at Bellhaven):** the names differ only by "The" and punctuation, so there is no rename. Grove City is renamed because its words differ.
- **Care type:** when a location lists two offerings, the first one that maps is used.
- **Phone:** never overwritten on an existing account. New accounts get the website phone.

## How I used AI

I used Claude Code as the engineering team and stayed in the decision seat:
- I set the requirements: the matching evidence order above, how the SOP must behave, and what the reviewer should see on each card.
- I had the tools write the code.
- I review each proposal myself before anything is written.

I also ran separate AI reviewers (Claude and Codex) against the code and the data. They caught real bugs, all now fixed:
- **A re-run would have retired the old CHOW accounts as duplicates.** That breaks the SOP. A test now covers it.
- **Holding a key could approve without the confirm step.**
- **Approve didn't re-check billing that changed after a proposal was made.** A test now covers it.

The judgment calls above are mine. Before approving, I went looking for any proposal that would be wrong to approve at a real company. That search turned up Ashtabula's PO box.

## What I'd build next

- Move proposals and decisions from `state.db` in git to a shared database, so several reviewers and the scheduled job don't conflict.
- Confirm suspected new owners (like Sandusky) from a second source, such as state licensing records, before linking.
- Add a reviewer name to each decision and note.
- Send an alert when a run produces an unusual number of proposals.
