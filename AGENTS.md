# Agent notes

The rules that apply to every change are below. Procedures for one area live in
`.agents/skills/` — plain `SKILL.md` files, so read one directly if your tooling
doesn't load skills (`.claude/skills` symlinks there for the ones that do):

| Working on | Read |
| --- | --- |
| `src/astrameter/ct002/`, `src/astrameter/status/`, `esphome/components/ct002/` | `.agents/skills/check-ct002-parity/SKILL.md` |
| anything under `web/`, or the docs screenshots | `.agents/skills/build-dashboard/SKILL.md` |
| balancer or simulator behavior | `.agents/skills/evaluate-steering/SKILL.md` |
| a `[SECTION]` config key, or `ha_addon/` | `.agents/skills/add-config-option/SKILL.md` |
| a powermeter backend | `.agents/skills/add-powermeter/SKILL.md` |

Keep all of it true: when a change makes something here or in a skill wrong,
fix it in the same change.

## Verify

Resolved versions live in **`uv.lock`**. Install dev dependencies the way CI
does, then run what CI runs (`.github/workflows/ci.yml`):

```bash
uv sync --extra dev
uv run ruff format . && uv run ruff check . && uv run mypy src/ && uv run pytest
```

Each skill above names the extra suite its area needs and how to get it running
in a sandbox. Don't report a suite as skipped without trying.

## Python ↔ ESPHome parity (REQUIRED)

`esphome/components/ct002/` is a mechanical C++ mirror of the Python CT002
stack: filenames, symbol names and filter ordering all match. Any change to
shared behavior lands on **both** sides in the same change, verified by `uv run
pytest tests/components/ct002/`. `CONTRIBUTING.md` maps file to file; the
`check-ct002-parity` skill has what is mirrored, what is deliberately waived, and what
the firmware constrains.

## Branches and pull requests

Open pull requests against **`develop`**, never `main` — tooling offers `main`
as the base, so set it yourself. `main` takes release merges and maintainer
hotfixes only, and `.github/workflows/pr-base-guard.yml` exempts maintainers, so
nothing will catch it for you. GitHub reads that workflow and
`.github/pull_request_template.md` from the **default branch**, so edits to either do nothing until they reach
`main` at the next release.

**Never post a top-level PR comment.** An agent posts under the maintainer's
account, so it reads as the maintainer pronouncing on their own pull request.
Status updates, verification results and "here's what I changed" summaries
belong in the **PR description**, edited as the work moves. The one place an
agent should write on GitHub is a **review thread**, to argue that a finding is
wrong — a finding you accept needs no reply, just the fix. Everything else you
say to the person you're working with.

## Changelog

For user-facing work, keep **exactly one bullet under `## Next`** for *your
change*. The unit is the change, not the branch or PR: a change spanning several
PRs edits that same bullet rather than adding one each. `## Next` holding
several bullets is normal and correct — one per change heading into the release
— but never touch a bullet belonging to a different change. Skip `CHANGELOG.md`
entirely for refactors, tooling and tests-only work.

Write it for the user: the visible problem and outcome, **one sentence of about
30 words**, no implementation details (internal names, config mechanics, parity
notes) unless the user genuinely sets the thing. Add a second sentence only when
they have to *do* something — set a new option, undo a workaround, adapt to a
break. Err on the side of terse.

Append `([#<pr>](https://github.com/tomquist/astrameter/pull/<pr>))` as soon as
you learn the number, without being asked.
