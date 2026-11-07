# House images

House images are committed directly as WebP files in `src/assets/house/`.
Import the `.webp` file from the page or component; no conversion step is needed.

When replacing an image, preserve its dimensions and transparency. The existing
images use colour quality 88 and lossless transparency. Keep original PNG renders
outside the repository to avoid increasing every user’s update download.
