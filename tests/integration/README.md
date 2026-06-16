# Phase 3 end-to-end harness

This harness proves that the **real** `src.agent` preview pipeline works against
real apps across the frameworks RenderPR supports. It is the integration-level
counterpart to the unit tests in `tests/test_agent/`: instead of monkeypatching,
it runs an actual dev server and HTTP-asserts what the browser/SSR would see.

## What it exercises

For each framework fixture the runner:

1. **Provisions** the app — copies the committed fixture tree (`--use-local`) or
   clones the private mirror repo (`spec.repo`).
2. **Detects** the stack with the real `discovery.discover_frontend(diff)` (the
   same call `main.py` makes), pointing `discovery.REPO_DIR` **and**
   `routes.REPO_DIR` at the clone. It asserts the detected `LaunchProfile`
   matches the fixture's expected package manager / framework / install command.
3. **Writes mocks** by dispatching through the real `mock_server` registry:
   `write_server_mocks` → `write_unmocked_fallbacks` → `write_banner` →
   `write_dev_origin_allowlist`.
4. **Installs** dependencies with the detected `install_command` (retry once).
5. **Boots** the dev server using the real `main._start_dev_process` +
   `main._resolve_ready_port` (NOT `_start_dev_server`, which `sys.exit`s and
   uses boto3).
6. **HTTP-asserts** runtime behavior against the live server.
7. **Tears down** in `finally`: stops the server, calls
   `mock_server.restore_runtime_files`, and removes the clone.

No agent logic is reimplemented — the harness only orchestrates real functions.

## The cells

Each fixture produces a `FixtureResult` whose `cells` map records `PASS` /
`FAIL` / `BLOCKED` per stage. A failed stage `BLOCKED`s the stages after it.

| cell | meaning |
|---|---|
| `install` | Stack detection matched expectations **and** dependencies installed. A detection mismatch (wrong PM/framework/install command) fails here. |
| `boot` | The dev server became reachable over HTTP before the boot timeout. |
| `host-bind` | The server answers on `0.0.0.0:<port>`, proving it bound all interfaces (not just localhost). |
| `server-mock-renders` | `page_path` SSR'd with the mock data (all `expected_items` present in the HTML) **and** `mocked_api` returns the 5-item mock JSON. |
| `unmocked-fallback` | `unmocked_api` degrades to `[]` with status 200 and the `x-renderpr-unmocked` header. |
| `banner` | The page references `/__renderpr-unmocked.js` and that script serves the `__renderprUnmockedInit` guard. |
| `routing` | Sanity: `second_page_path` renders and contains the shared `ItemList component` marker. |

**Overall pass** for a fixture iff every non-`BLOCKED` cell is `PASS` (and at
least one cell ran). The sweep exits 0 only if every fixture passes.

## Fixtures

Fixtures live in `fixtures/<fw>/`:

- `spec.py` exposes a module-level `SPEC: FixtureSpec` (see `spec_types.py`).
- The rest of the directory is the app source tree, a mirror of the private
  per-framework repo (`spec.repo`). `--use-local` runs against this committed
  copy; the default Docker flow clones the mirror.

Known frameworks (run order): `sveltekit`, `astro`, `remix`, `next`. They run
**serially** because they share dev-server ports.

## Running

Local host run (fast iteration; needs node + pnpm/yarn/npm on PATH):

```bash
tests/integration/run.sh --use-local
tests/integration/run.sh --use-local --frameworks sveltekit,remix
```

Docker run (production-shaped image; clones private mirrors, needs a GH token):

```bash
tests/integration/run.sh                    # all frameworks
tests/integration/run.sh --frameworks next  # subset
```

Or invoke the entrypoint directly:

```bash
python -m tests.integration.entrypoint --frameworks all --use-local --out ./_out
```

Under Docker, equivalent to what `run.sh` does:

```bash
docker build -t renderpr:harness .
docker run --rm \
  -e GH_TOKEN="$(gh auth token)" \
  -v "$PWD:/work" -w /work renderpr:harness \
  python -m tests.integration.entrypoint --frameworks all --out /work/tests/integration/_out
```

## Output

The entrypoint writes to `--out`:

- `report.md` — a Markdown matrix (columns: install, boot, host-bind,
  server-mock-renders, unmocked-fallback, banner) plus per-fixture error/log
  details for anything that failed.
- `<framework>.log` — per-fixture captured logs for failed cells.

It also prints a console table to stdout and exits 0 only when all fixtures pass.
