# Predbat Modern UI

The Modern UI is a React and TypeScript application built with Vite. It is
served by the development server or by Predbat from the generated
`apps/predbat/predbat_ui*` files.

## Start developing

```bash
cd frontend
npm install
npm run dev
```

Open the address printed by Vite. Run Predbat locally as well when testing
live data; the frontend reads its APIs relative to the current Predbat URL, so
the same build works through Home Assistant ingress.

## Check a change

```bash
npm test
npm run lint
npm run build
```

`npm run build` writes flat files with stable `predbat_ui` names directly into
`../apps/predbat/`. It replaces only files with that reserved prefix and preserves
the backend files beside them. `npm run bundle` is an alias for the same build.
Run the build and include its generated files before testing in Home Assistant
or opening a pull request. CI rebuilds them and checks for differences.

Assets are served with conditional caching and compression for text files.
Stable filenames overwrite older versions during Predbat updates.

## Useful project locations

| Location | Purpose |
| --- | --- |
| `src/pages/` | Full-page views and route-level UI |
| `src/components/` | Reusable UI components and their CSS |
| `src/types/` | TypeScript contracts for Predbat API data |
| `src/utils/` | Formatting and data transformation helpers |
| `tests/` | Node-based component and utility tests |
| `tests/fixtures/` | Anonymised API examples for repeatable tests |
| `docs/` | Frontend API and design conventions |

## Working conventions

- From modern UI pages under `./ui/`, use `../api/plan_data` for backend API
  calls and `../` for POST and download endpoints. Keep page navigation relative
  to `./`, so it stays within the modern UI and works through Home Assistant ingress.
- Add or update a fixture when an API field has important display behaviour.
- Prefer a focused regression test for a reported UI problem.
- Do not edit the generated `apps/predbat/predbat_ui*` files directly.
- Read [the API contract](docs/api-contract.md) and
  [UI conventions](docs/ui-conventions.md) before changing a page.
