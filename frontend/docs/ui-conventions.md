# Modern UI conventions

## Meaningful colours

- **Blue**: battery charging and charge actions.
- **Purple**: export power and export actions.
- **Yellow**: solar generation and solar forecast.
- **Green**: normal live demand, completed/healthy state, and enabled controls.
- **Muted grey**: inactive, unavailable, or zero-power values.

Use the existing CSS custom properties instead of introducing page-specific
colour values.

## Plan table

- The table remains horizontally scrollable on narrow screens; do not clip
  cells or hide data to force it to fit.
- Use compact column labels on mobile.
- Actions remain legible pills. A split slot displays one pill for each planned
  action.
- Tooltips explain an action or event without duplicating visible labels.
- Keep manual-override controls available on future slots only.

## Responsive layout

- Pages must not create horizontal scrolling on the document itself. Any wide
  content belongs in a deliberately scrollable container.
- Cards in a group should share a width and align their primary values.
- Avoid fixed widths where a grid or flex layout can adapt to Home Assistant
  panels, desktop windows, and mobile screens.

## Accessibility and interaction

- Every icon-only control needs an accessible name or visible tooltip.
- Keep visible keyboard focus states.
- Do not rely on colour alone to convey a planned action or warning.
- Respect the existing light, dark and automatic theme settings.
