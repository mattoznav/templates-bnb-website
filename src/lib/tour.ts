// Shared state between the scroll script and the 3D scene.
// `target` is a position along the tour: 0 is the first stop, 1 the second,
// 2.5 halfway between the third and the fourth, and so on.

type Listener = () => void;

export const tour = {
  target: 0,
  /** Set by the scene when someone clicks a room in the model */
  onRoomClick: null as ((slug: string) => void) | null,
  ready: false,
};

const readyListeners = new Set<Listener>();

export function markReady() {
  tour.ready = true;
  readyListeners.forEach((listener) => listener());
}

export function onReady(listener: Listener) {
  if (tour.ready) listener();
  else readyListeners.add(listener);
}

/** Whether the device can show the 3D tour at all */
export function canShowTour() {
  if (typeof window === "undefined") return false;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}
