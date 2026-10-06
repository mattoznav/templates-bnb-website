# B&B website with a 3D tour

The public website of "Alder House", a fictional five-room bed & breakfast. The page is a guided tour of a 3D model of the house: as you scroll, the roof lifts off and the camera moves from room to room, while each room's description, photo, amenities and price slide in next to it. A floor plan and the model itself are clickable, so guests can also jump straight to a room.

There is no booking engine and no backend: every "Book" button links to the property's existing booking system (Booking.com, Airbnb, a channel manager or its own engine).

| | |
| --- | --- |
| Framework | [Astro](https://astro.build) 7, static output |
| 3D | [three.js](https://threejs.org) through [React Three Fiber](https://r3f.docs.pmnd.rs) and drei, in a single React island |
| Scroll | [Lenis](https://lenis.darkroom.engineering) for smooth scrolling, [GSAP](https://gsap.com) ScrollTrigger for the card animations |
| Model | Built by a Python script in [Blender](https://www.blender.org) 5.2 and dressed with CC0 assets from [Poly Haven](https://polyhaven.com); lighting baked with Cycles, exported as glTF with Draco and WebP |
| Runs on | `localhost:4324` |
| Live demo | [mattoznav.github.io/templates-bnb-website](https://mattoznav.github.io/templates-bnb-website/) |

## Requirements

| Tool | Version | Needed for |
| --- | --- | --- |
| Node.js and npm | Node 22.12 or newer | running and building the site |
| Blender | 5.2 or newer | only to change the house and rebuild the model |

The built model and the room photos are committed, so Blender is not needed to run the site.

## Install and run

```bash
npm install
npm run dev
```

Open `http://localhost:4324`.

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server with hot reload |
| `npm run build` | Static site in `dist/`, ready for any static hosting |
| `npm run preview` | Serves the built `dist/` locally |
| `npm run check` | Type-checks the Astro, TypeScript and React files |
| `npm run assets` | Downloads the CC0 models, textures and sky used by the model (once, about 850 MB) |
| `npm run model` | Rebuilds the 3D model and the room photos with Blender (see below) |

## How the tour works

1. `src/pages/index.astro` renders one `<section>` per stop of the tour, listed in `src/data/stops.ts`. The sections are normal HTML: the content is readable, indexable and works without JavaScript.
2. `src/scripts/scroll.ts` turns the scroll position into a position along the tour: `2` when the third section is centred on screen, `2.5` halfway to the fourth. It also highlights the room in view on the floor plans and animates the cards.
3. `src/components/tour/Tour.tsx` reads that position every frame. The camera moves between the `camera` and `target` points of the two nearest stops, eased so it settles at each one. The roof lifts and fades between the first and second stop.
4. The house is drawn without lights: every surface shows a texture that already contains its lighting, baked in Blender and denoised. Screens wider than about 2000 physical pixels load `alder-house-hd.glb` (lightmaps up to 4096 px, about 9 MB); phones and smaller screens load `alder-house.glb` (half resolution, about 5.5 MB). Trees are drawn as "impostors", crossed cards carrying a render of the real tree, so millions of leaves cost a few triangles. Potted plants keep their own textures.

If the device cannot run WebGL, or the visitor prefers reduced motion, the 3D is skipped and the page shows the rendered photos over a still image of the house.

## Make it your own

| What | Where |
| --- | --- |
| Name, village, contact, check-in times, currency | `src/data/site.ts` |
| Booking link for every button | `bookingUrl` in `src/data/site.ts`, or per room in `src/data/stops.ts` |
| Rooms: text, facts, amenities, price, photo | `src/data/stops.ts` |
| Camera position of each stop | `camera` and `target` in `src/data/stops.ts` (metres, same axes as the model) |
| Room layout for the floor plan and the model | `src/data/house.json` |
| Colours and fonts | `src/styles/global.css` |
| Address, travel times, "Open in Maps" link | `address`, `travel` and `mapsUrl` in `src/data/site.ts` |
| Domain | `site` in `astro.config.mjs` |
| Links to files in `public/` | wrap the path in `withBase()` from `src/lib/paths.ts`, so the site also works under a sub-path |

Coordinates follow the model: `x` runs along the front of the house, `y` from the front to the back and `z` up, with the front left corner of the house at `0, 0, 0`.

### The "Getting here" map

The map at the bottom of the page (`src/components/Location.astro`) is an illustration of the fictional village, drawn as an inline SVG, with one route per way of arriving: pointing at "By train", "By car" or "On foot" highlights that route. For a real property, set `mapsUrl` to a Google Maps or Apple Maps share link and either redraw the SVG for the real area or replace the figure with an image or an embedded map.

### Using a scan of a real house

For a real property, the code-built house can be replaced with a scan of the actual rooms, made with a phone app that exports glTF (photogrammetry or Gaussian splatting). Keep the same pattern: one mesh per room named `room_<slug>` so clicks on the model jump to that room, a mesh named `roof` if the tour should lift it, and the camera points in `stops.ts` moved to match. Compress the file with Draco and keep textures at 2048 px or less so it still loads quickly on mobile.

## Rebuilding the model

`model/build_house.py` builds the whole scene from code: walls with their window and door openings, sash windows with stone sills and shutters, gutters and downpipes, the porch, the roof and chimney, hedges, the picket fence and the terrace. It furnishes the rooms with procedural pieces (beds, kitchen, fireplace, curtains, radiators, paintings) and CC0 models from Poly Haven (sofas, armchairs, tables, chairs, cabinets, lamps, plants, trees), and textures every surface with Poly Haven PBR materials. It then:

1. renders one photo per room with Cycles into `public/media/rooms/`
2. bakes the light of each part into its own texture (walls, garden, roof and one per room) and denoises it with Open Image Denoise
3. renders each tree from the side and from above for its impostor cards
4. exports `public/models/alder-house.glb` with Draco geometry and WebP textures

```bash
npm run assets                         # once: downloads the CC0 assets into model/assets/
npm run model                          # full quality, about 75 minutes on a laptop GPU
npm run model -- --quick               # low resolution, a few minutes, for checking changes
npm run model -- --skip-bake           # photos only
npm run model -- --skip-photos         # model only
```

`npm run assets` needs Python 3 and an internet connection; the files land in `model/assets/`, which git ignores. `npm run model` expects `blender` on the `PATH`. On macOS with Homebrew: `brew install --cask blender`. The script reads the room layout from `src/data/house.json`, so the floor plan on the site and the model always agree; the furniture positions are in the script itself.

## Publish on GitHub Pages

`.github/workflows/pages.yml` builds the site and publishes it on GitHub Pages at every push to `main`. To use it in a copy of the repository, open **Settings > Pages** and set **Source** to **GitHub Actions**. The workflow passes the Pages address to the build through `SITE_URL` and `BASE_PATH`, so the site works under `https://<user>.github.io/<repository>/`; with a custom domain the path is simply `/`.

## Structure

```
model/build_house.py       Blender script: model, photos, bake, export
model/fetch_assets.py      Downloads the CC0 assets the model uses
public/models/             The exported model
public/media/rooms/        Room photos rendered from the model
public/draco/              Draco decoder used by the glTF loader
src/data/                  Site settings, tour stops, house layout
src/components/tour/       The React Three Fiber scene
src/components/            Floor plan and the "Getting here" section
src/scripts/scroll.ts      Scroll position, floor plan highlight, card animations
src/lib/paths.ts           withBase(), for paths that must follow the site's base
src/pages/                 The tour page and the 404 page
.github/workflows/         GitHub Pages deployment
```

## Assets and licenses

The code is released under the MIT License.

- **3D assets, textures and sky:** [Poly Haven](https://polyhaven.com), all [CC0](https://polyhaven.com/license). The full list is in `model/fetch_assets.py`: furniture, lamps and plants, trees, 21 PBR textures (stucco, plaster, oak and herringbone floors, terracotta tiles, clay roof tiles, stone, gravel, grass, fabrics, marble) and the `kloofendal_48d_partly_cloudy_puresky` sky.
- **Everything else in the model** (walls, windows, roof, beds, kitchen, hedges, paintings) is generated by `model/build_house.py`, and the room photos are rendered from it.
- **Icons:** [Phosphor](https://phosphoricons.com), MIT.
- **Fonts:** Newsreader and Manrope, SIL Open Font License, from Fontsource.
- **Draco decoder** in `public/draco/`: from three.js, Apache 2.0.

Alder House, Larchford, Mill Lane, Larch Wood, Larch Brook and Hollins Farm are fictional.
