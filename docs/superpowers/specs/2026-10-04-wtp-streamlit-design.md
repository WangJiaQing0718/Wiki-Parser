# WTP Wikitext Playground Design

## Goal

Add a small local Streamlit page to the Wiki Parser project. A user can enter a page title and Wikitext, run the bundled WikitextProcessor (WTP), and inspect both the expanded Wikitext and readable plain text.

## Current project context

- WTP is included at `wikitextprocessor/` and is installed from the root `requirements.txt`.
- `Wikipedia-Parser-dev/pipeline/wtp_integration.py` already provides `expand_to_text()` and `expanded_wikitext_to_text()`.
- Template and Lua expansion depends on a prebuilt WTP SQLite database. The repository has a database under `WikiData/`; the app must not build a database automatically.
- The working tree already contains unrelated modifications. Implementation must keep changes limited to the new app, its dependency/documentation, and any narrowly required integration.

## Proposed interface

Create a Streamlit app under `Wikipedia-Parser-dev/` with:

- A database path field, defaulting to a matching `*-wtp-full.db` file found under the repository's `WikiData/` directory when available.
- A page title field defaulting to `Sandbox`, so WTP parser functions can resolve page context.
- A Wikitext input area and an explicit expand button.
- Two result panels or tabs: expanded Wikitext and plain text.
- Clear progress and error messages for a missing database or WTP expansion failure.

The page is intended for local use and will bind to localhost by default. It uses an existing WTP database and never builds or replaces one.

## Data flow and implementation

1. Streamlit reads the selected database path and validates that it exists.
2. A cached WTP instance is opened for the selected database with the existing project/language defaults (`wikipedia` / `en`). Changing the database path creates a distinct cached instance.
3. On submit, the app calls `start_page(page_title)` and expands the entered Wikitext with WTP, using the existing configured expansion timeout where practical.
4. The raw expansion is shown unchanged in the expanded result panel. The existing `expanded_wikitext_to_text()` helper converts it to readable text for the second panel.
5. Expansion warnings/errors are surfaced beside the outputs instead of being hidden in the server console.

The app will reuse the existing conversion helper rather than maintain a second implementation. It will not trigger dump processing or database creation.

## Files and dependencies

- Add `Wikipedia-Parser-dev/wtp_playground.py` as the Streamlit entry point.
- Add Streamlit to the root `requirements.txt` so the app can run in the repository's existing Python environment.
- Add a short run instruction to `Wikipedia-Parser-dev/README.md`.

## Acceptance criteria

- Starting the documented Streamlit command opens the local page.
- A user can enter Wikitext, submit it, and see WTP's expanded result and the corresponding plain text.
- A user can select a different existing WTP database and page title.
- Missing database paths and expansion errors are shown in the page with actionable messages.
- The app does not create, overwrite, or migrate WTP databases.

## Scope

No authentication, remote hosting, dump building, database management, or HTML rendering is included. Automated tests are not part of this request.
