// The tour, in order. Each stop is one scroll section and one camera position.
// Coordinates are in metres, in the same system as house.json and the Blender
// script: x runs along the front of the house, y from the front to the back,
// z up. The camera sits at `camera` and looks at `target`.

type Point = [x: number, y: number, z: number];

export type Stop = {
  slug: string;
  kind: "intro" | "common" | "room" | "outro";
  camera: Point;
  target: Point;
  eyebrow: string;
  title: string;
  text: string;
  photo?: string;
  facts?: string[];
  amenities?: string[];
  /** Price per night, from */
  price?: number;
  bookingUrl?: string;
};

export const stops: Stop[] = [
  {
    slug: "arrival",
    camera: [-1, -16, 3.2], target: [3, 4, 2.4],
    kind: "intro",
    eyebrow: "Larchford",
    title: "A quiet house at the edge of the wood",
    text: "Three bedrooms, a fire in the lounge and breakfast at one long table. Scroll to walk through the house, room by room.",
  },
  {
    slug: "overview",
    camera: [8, -6.5, 17], target: [8, 4.2, 0],
    kind: "intro",
    eyebrow: "The house",
    title: "One floor, five rooms",
    text: "Everything is on the ground floor: two shared rooms at the front, three bedrooms around them, each with its own window onto the garden. Pick a room on the plan, or keep scrolling.",
  },
  {
    slug: "lounge",
    camera: [3, -2.2, 11.5], target: [3, 2.7, 0.3],
    kind: "common",
    eyebrow: "Shared",
    title: "The Lounge",
    text: "Where you arrive and where most evenings end: a wood fire, a deep leather sofa and two old armchairs. Keys and local maps wait on the shelves by the door.",
    photo: "/media/rooms/lounge.webp",
    amenities: ["Wood fire from October to April", "Honesty bar", "Board games and books", "Fast Wi-Fi"],
  },
  {
    slug: "breakfast-room",
    camera: [8.5, -2.2, 11.5], target: [8.5, 2.7, 0.3],
    kind: "common",
    eyebrow: "Shared",
    title: "The Breakfast Room",
    text: "One table for everyone. Bread is baked in the village every morning, eggs come from the farm down the lane and the jam is made here in late summer.",
    photo: "/media/rooms/breakfast-room.webp",
    facts: ["Breakfast 7:30 to 10:30"],
    amenities: ["Included in every stay", "Vegetarian and gluten-free on request", "Tea and coffee all day"],
  },
  {
    slug: "garden-room",
    camera: [13.5, -2.2, 11.5], target: [13.5, 2.7, 0.3],
    kind: "room",
    eyebrow: "Room 1",
    title: "The Garden Room",
    text: "A double room with a wide window that opens straight onto the lawn. Morning light, a reading chair and the sound of the wood behind the fence.",
    photo: "/media/rooms/garden-room.webp",
    facts: ["Sleeps 2", "24 m²", "Double bed"],
    amenities: ["Garden view", "En-suite shower room", "Dresser and mirror", "Linen curtains"],
    price: 140,
  },
  {
    slug: "linen-room",
    camera: [12.4, 2.8, 12.5], target: [12.4, 7.8, 0.3],
    kind: "room",
    eyebrow: "Room 2",
    title: "The Linen Room",
    text: "Two single beds under two windows: right for friends walking the trails, or for a parent and a child. The desk faces the orchard at the back.",
    photo: "/media/rooms/linen-room.webp",
    facts: ["Sleeps 2", "33 m²", "Twin beds"],
    amenities: ["Orchard view", "En-suite shower room", "Writing desk", "Space for a travel cot"],
    price: 120,
  },
  {
    slug: "courtyard-suite",
    camera: [6.6, 2.6, 13], target: [6.7, 7.8, 0.3],
    kind: "room",
    eyebrow: "Room 3",
    title: "The Courtyard Suite",
    text: "The largest room in the house: a king-size bed, a sofa by the window and its own bathroom with a freestanding bath. The terrace at the back of the house is a few steps away.",
    photo: "/media/rooms/courtyard-suite.webp",
    facts: ["Sleeps 2", "42 m²", "King-size bed"],
    amenities: ["Bathroom with a freestanding bath", "Sofa and work desk", "Terrace access", "Late check-out included"],
    price: 210,
  },
  {
    slug: "departure",
    camera: [22, -14, 5], target: [8, 4, 1.5],
    kind: "outro",
    eyebrow: "Plan your stay",
    title: "Come and stay",
    text: "Larchford is an hour by train from the city, then a ten-minute walk up the lane. We can pick you up from the station if you let us know your train.",
  },
];

export const rooms = stops.filter((stop) => stop.kind === "room" || stop.kind === "common");

/** Which side of the screen a stop's text sits on: rooms alternate, starting on the right */
export function cardSide(stop: Stop): "left" | "right" {
  const index = rooms.indexOf(stop);
  return index >= 0 && index % 2 === 0 ? "right" : "left";
}
