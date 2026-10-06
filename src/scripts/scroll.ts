// Turns the page scroll into a position along the tour, keeps the floor plans
// in sync and animates the room cards in.
import Lenis from "lenis";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { tour } from "../lib/tour";

gsap.registerPlugin(ScrollTrigger);

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const sections = [...document.querySelectorAll<HTMLElement>("[data-stop]")];
const planRooms = [...document.querySelectorAll<SVGAElement>("[data-plan-room]")];

const lenis = reduceMotion ? null : new Lenis({ autoRaf: true, lerp: 0.09 });
lenis?.on("scroll", ScrollTrigger.update);

let centers: number[] = [];
function measure() {
  centers = sections.map((section) => section.offsetTop + section.offsetHeight / 2);
}

/** Tour position for the current scroll: 2 when the third section is centred, 2.5 halfway to the fourth */
function positionAt(scroll: number) {
  const middle = scroll + window.innerHeight / 2;
  if (middle <= centers[0]) return 0;
  for (let i = 0; i < centers.length - 1; i++) {
    if (middle < centers[i + 1]) return i + (middle - centers[i]) / (centers[i + 1] - centers[i]);
  }
  return centers.length - 1;
}

let activeSlug = "";
function update() {
  tour.target = positionAt(window.scrollY);
  const slug = sections[Math.round(tour.target)]?.id ?? "";
  if (slug === activeSlug) return;
  activeSlug = slug;
  document.documentElement.dataset.stop = slug;
  for (const room of planRooms) {
    room.classList.toggle("is-active", room.dataset.planRoom === slug);
  }
}

function scrollToStop(slug: string) {
  const section = document.getElementById(slug);
  if (!section) return;
  // Tour stops land centred, like the camera; other sections land at their top
  const top = section.hasAttribute("data-stop")
    ? section.offsetTop + section.offsetHeight / 2 - window.innerHeight / 2
    : section.getBoundingClientRect().top + window.scrollY - (document.querySelector(".topbar")?.clientHeight ?? 0);
  if (lenis) lenis.scrollTo(top, { duration: 1.8 });
  else window.scrollTo({ top });
  history.replaceState(null, "", `#${slug}`);
}

tour.onRoomClick = scrollToStop;

// Same-page links (nav, floor plans, "back to the start") land with their section centred
document.addEventListener("click", (event) => {
  const link = (event.target as Element).closest<HTMLAnchorElement | SVGAElement>("a[href]");
  if (!link) return;
  const href = link.getAttribute("href") ?? "";
  const hash = href.startsWith("#") ? href.slice(1) : href.startsWith("/#") ? href.slice(2) : "";
  if (!hash || !document.getElementById(hash)) return;
  event.preventDefault();
  scrollToStop(hash);
});

function relayout() {
  measure();
  update();
  ScrollTrigger.refresh();
}

measure();
update();
window.addEventListener("resize", relayout);
// Without the 3D the sections get shorter: re-measure when the tour decides
new MutationObserver(relayout).observe(document.documentElement, { attributeFilter: ["data-tour"] });
// Follow the scroll through ScrollTrigger, which batches updates once per frame
ScrollTrigger.create({ start: 0, end: "max", onUpdate: update });

if (location.hash) {
  requestAnimationFrame(() => scrollToStop(location.hash.slice(1)));
}

if (!reduceMotion) {
  for (const card of document.querySelectorAll<HTMLElement>("[data-reveal]")) {
    gsap.from(card, {
      y: 60,
      opacity: 0,
      duration: 0.9,
      ease: "power3.out",
      scrollTrigger: { trigger: card, start: "top 85%", toggleActions: "play none none reverse" },
    });
  }
  gsap.from(".hero > *", { y: 24, opacity: 0, duration: 1, ease: "power3.out", stagger: 0.08, delay: 0.2 });
}
