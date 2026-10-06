/**
 * Prefix a root-relative path with the folder the site is served from:
 * "/" locally, "/<repository>" on GitHub Pages (see astro.config.mjs).
 */
export const withBase = (path: string) => `${import.meta.env.BASE_URL.replace(/\/$/, "")}${path}`;
