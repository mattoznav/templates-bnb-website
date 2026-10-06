// Everything a new property needs to change lives in this file and in stops.ts.
// The brand, the village and every detail below are fictional.

export const site = {
  name: "Alder House",
  tagline: "A five-room bed & breakfast at the edge of Larch Wood",
  description:
    "Alder House is a small bed & breakfast in the village of Larchford: three bedrooms, a lounge with a fireplace and a long breakfast table. Walk through the house in 3D, room by room.",
  village: "Larchford",
  email: "stay@example.com",

  // Where every "Book" button goes. Point it at Booking.com, Airbnb or your own
  // booking engine. Rooms can override it with their own `bookingUrl`.
  bookingUrl: "https://booking.example.com/alder-house",

  // Shown in the "Getting here" section. Replace mapsUrl with a link from
  // Google Maps or Apple Maps ("Share" > "Copy link") for the real address.
  address: ["Alder House", "Mill Lane", "Larchford"],
  mapsUrl: "https://maps.example.com/?q=Alder+House+Larchford",
  travel: [
    { mode: "train", title: "By train", time: "1 hr", detail: "From the city to Larchford, then a 10 minute walk up Mill Lane. We can meet you at the station." },
    { mode: "car", title: "By car", time: "1 hr 15", detail: "Leave the main road at the Larchford sign. Free parking for every room, behind the hedge." },
    { mode: "walk", title: "On foot", time: "5 min", detail: "To the edge of Larch Wood and the river path. The village bakery is 8 minutes away." },
  ],

  checkIn: "3 pm to 8 pm",
  checkOut: "by 11 am",
  breakfast: "7:30 to 10:30, served at the long table",
  currency: "EUR",
} as const;

export function bookingLink(room?: { slug: string; bookingUrl?: string }) {
  if (room?.bookingUrl) return room.bookingUrl;
  if (!room) return site.bookingUrl;
  return `${site.bookingUrl}?room=${encodeURIComponent(room.slug)}`;
}

export function formatPrice(amount: number) {
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: site.currency,
    maximumFractionDigits: 0,
  }).format(amount);
}
