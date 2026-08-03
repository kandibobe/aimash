# Research archive v1

The archive is a small evidence cache for a private agency. It is not model fine-tuning, an autonomous
learning loop, or client memory. It stores versioned arXiv metadata and abstracts so Hermes can retrieve
sources later without changing Google Ads or client profiles.

## Boundary

- `archive_import_arxiv(account, query, limit)` queries only the fixed arXiv Atom endpoint and stores
  at most 25 metadata records. It does not download PDFs.
- `archive_search(account, query, limit)` performs local title/abstract search.
- Both tools require an account that already passes the normal read allow-list. The account is an access
  anchor only; research rows are agency-global and are never copied to client memory automatically.
- Returned title and abstract text are marked `trust=external`; numbers from them are not added to the
  fact guard allow-list.
- `RESEARCH_ARCHIVE_ENABLED=false` is the default. In that state both tools are absent from MCP.

Hermes treats `/archive <query>` as the intent for these tools. The actual dashboard slash-command
dispatch still requires a live UAT after deployment; a local prompt contract test does not prove that
the deployed Hermes command router forwards an unknown slash command.

## Enable

Apply migration `0042_research_archive`, set `RESEARCH_ARCHIVE_ENABLED=true`, restart the Aimash MCP
service, and verify that exactly `archive_search` and `archive_import_arxiv` appear in the Hermes tool
surface. Keep the flag off if the live slash-command UAT does not reach the tools.

## Rollback

Prefer the non-destructive rollback: set `RESEARCH_ARCHIVE_ENABLED=false` and restart MCP. Existing
research rows remain inert and can be re-enabled later.

Code changes are split into atomic commits and can be reverted independently:

1. Revert the research archive commit to remove the tools and prompt contract.
2. Revert `0de15cb` to remove only the offline trace benchmark.
3. Revert `432ee47` to remove only the dashboard/background UX contract.

Only if the archive data is intentionally disposable, downgrade Alembic from `0042_research_archive`
to `0041_proposal_outcomes`. That drops `research_sources` and is destructive; disabling the flag is the
normal rollback.
