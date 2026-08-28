# CareerOS core rewrite record

## Scope

The following CareerOS interfaces were reimplemented while preserving the
existing local data format and GUI contracts:

- `Database.upsert_job`
- `JobService.search`
- `JobService.analyze`
- `ResumeService.optimize`
- `CoverLetterService.generate`

The rewrite also replaces the surrounding repository methods required by those
interfaces: manual/CSV/JSON job intake, manual application tracking, resume
draft records, cover-letter records, and supporting-document storage.

## Functional specification used

- A job is identified first by a non-empty saved URL, then by company, title,
  and location when the source has no usable URL.
- New source descriptions refresh their own facts but do not erase an existing
  salary or stated start date with a blank source value.
- Preliminary scoring is deterministic and based on visible candidate/posting
  terms, seniority signals, and saved location preferences.
- Final scoring combines the deterministic score with the configured AI share.
- Resume/CV generation uses only the original resume and verified local facts;
  high-risk changes are not applied automatically.
- Cover letters remain drafts. No service navigates, fills, uploads, or submits
  an application form.

## Compatibility and verification

- Existing SQLite table names and columns remain readable.
- Existing Settings, GUI imports, and public service method names are retained.
- `python -m unittest discover -s tests -v` passed 32 tests after the rewrite.
- An offscreen MainWindow startup check passed against a new temporary data
  directory.

## Provenance limitation

This is an independently written replacement of the CareerOS implementation.
It is not a legal certification of a strict two-person clean-room process:
the code-review environment had previously inspected the public upstream
repository while assessing provenance. Any commercial licensing decision
should be reviewed by qualified legal counsel and, if necessary, followed by
a separate implementation team working only from this functional specification.
