# Predbat Modern UI

The Modern UI is a React and TypeScript application built with Vite. It is
served by Predbat either from the development server or from the bundled
`apps/predbat/frontend.zip` archive.

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

`npm run bundle` runs the production build and packages `dist/` as
`../apps/predbat/frontend.zip`. Run it before testing a change in Home
Assistant or opening a pull request that changes frontend behaviour.

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

- Keep API paths relative, for example `./api/plan_data`, so Home Assistant
  ingress continues to work.
- Add or update a fixture when an API field has important display behaviour.
- Prefer a focused regression test for a reported UI problem.
- Do not edit the generated `dist/` directory directly.
- Read [the API contract](docs/api-contract.md) and
  [UI conventions](docs/ui-conventions.md) before changing a page.
