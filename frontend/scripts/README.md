# House image conversion

Place the original PNG renders in `src/assets/house/`. `npm run build` and
`npm run dev` generate matching `.webp` files before starting. To convert manually,
run `npm run images:house` from `frontend/`.

Import the `.webp` file from the page or component. Conversion preserves pixel
dimensions and lossless transparency, with colour quality 88. PNG masters are
kept intact; generated WebP files are ignored by Git. Unchanged images are skipped.
Replacing a PNG or changing the converter regenerates its WebP copy.

Conversion runs at startup, so after adding or replacing PNGs during a development
session, run `npm run images:house` again. New filenames still need to be referenced
by the relevant page. Build environments must have the PNG masters available.
