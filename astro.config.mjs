import { defineConfig } from "astro/config";
import react from "@astrojs/react";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  // Replace with the real domain before going live
  site: "https://alderhouse.example.com",
  server: { port: 4324 },
  integrations: [react(), sitemap()],
  // Keep demos clean when showing the template
  devToolbar: { enabled: false },
});
