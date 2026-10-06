import { defineConfig } from "astro/config";
import react from "@astrojs/react";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  // Replace with the real domain before going live. The GitHub Pages workflow sets
  // SITE_URL and BASE_PATH to publish the demo under the repository's path.
  site: process.env.SITE_URL ?? "https://alderhouse.example.com",
  base: process.env.BASE_PATH || "/",
  server: { port: 4324 },
  integrations: [react(), sitemap()],
  // Keep demos clean when showing the template
  devToolbar: { enabled: false },
});
