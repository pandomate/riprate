/* Shared monochrome SVG icons (material-style paths) — the one place every
   page pulls its glyphs from, so nothing renders as a colored emoji square.
   All icons inherit currentColor and size via the `size` prop. */

export function Svg({ children, size = 18, stroke = false }: {
  children: React.ReactNode; size?: number; stroke?: boolean;
}) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size}
      fill={stroke ? "none" : "currentColor"}
      stroke={stroke ? "currentColor" : "none"}
      strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
      className="inline-block flex-shrink-0 align-[-0.15em]">
      {children}
    </svg>
  );
}

type P = { size?: number };

export const IPlay = ({ size }: P) =>
  <Svg size={size}><path d="M8 5v14l11-7z" /></Svg>;
export const IPause = ({ size }: P) =>
  <Svg size={size}><path d="M6 5h4v14H6zM14 5h4v14h-4z" /></Svg>;
export const IDownload = ({ size }: P) =>
  <Svg size={size}><path d="M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z" /></Svg>;
export const IZip = ({ size }: P) =>
  <Svg size={size}><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 2h2v2h-2V8zm-2 2h2v2h-2v-2zm2 2h2v2h-2v-2zm-2 2h2v2h-2v-2z" /></Svg>;
export const ITrash = ({ size }: P) =>
  <Svg size={size}><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" /></Svg>;
export const IPencil = ({ size }: P) =>
  <Svg size={size}><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" /></Svg>;
export const IClose = ({ size }: P) =>
  <Svg size={size} stroke><path d="M6 6l12 12M18 6L6 18" /></Svg>;
export const ICheck = ({ size }: P) =>
  <Svg size={size} stroke><path d="M4 12.5l5 5L20 6.5" /></Svg>;
export const IRetry = ({ size }: P) =>
  <Svg size={size}><path d="M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" /></Svg>;
export const ICalendar = ({ size }: P) =>
  <Svg size={size}><path d="M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zM5 8V6h14v2H5z" /></Svg>;
export const IFolder = ({ size }: P) =>
  <Svg size={size}><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z" /></Svg>;
export const IFilm = ({ size }: P) =>
  <Svg size={size}><path d="M18 3v2h-2V3H8v2H6V3H4v18h2v-2h2v2h8v-2h2v2h2V3h-2zM8 17H6v-2h2v2zm0-4H6v-2h2v2zm0-4H6V7h2v2zm10 8h-2v-2h2v2zm0-4h-2v-2h2v2zm0-4h-2V7h2v2z" /></Svg>;
export const ITv = ({ size }: P) =>
  <Svg size={size}><path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z" /></Svg>;
export const IHeart = ({ size, filled = false }: P & { filled?: boolean }) =>
  filled
    ? <Svg size={size}><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" /></Svg>
    : <Svg size={size}><path d="M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3zm-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z" /></Svg>;
export const IChevron = ({ dir, size = 18 }: { dir: "left" | "right" | "up" | "down"; size?: number }) =>
  <Svg size={size} stroke>
    {dir === "left" ? <path d="M15 6l-6 6 6 6" />
      : dir === "right" ? <path d="M9 6l6 6-6 6" />
      : dir === "up" ? <path d="M6 15l6-6 6 6" />
      : <path d="M6 9l6 6 6-6" />}
  </Svg>;
export const IRestart = ({ size }: P) =>
  <Svg size={size}><path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z" /></Svg>;
export const ISearch = ({ size }: P) =>
  <Svg size={size}><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z" /></Svg>;
export const IGearNav = ({ size }: P) =>
  <Svg size={size}><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" /></Svg>;
export const IUsers = ({ size }: P) =>
  <Svg size={size}><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5s-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z" /></Svg>;
export const IList = ({ size }: P) =>
  <Svg size={size}><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z" /></Svg>;
export const IInbox = ({ size }: P) =>
  <Svg size={size}><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 12h-4c0 1.66-1.34 3-3 3s-3-1.34-3-3H5V5h14v10z" /></Svg>;
export const IActivity = ({ size }: P) =>
  <Svg size={size} stroke><path d="M22 12h-4l-3 8L9 4l-3 8H2" /></Svg>;
export const ISignal = ({ size }: P) =>
  <Svg size={size}><path d="M2 22h20V2L2 22zm18-2h-2.5v-2.5H20V20zm0-4.5h-2.5V13H20v2.5zM20 11h-2.5V8.5H20V11z" /></Svg>;
export const IBox = ({ size }: P) =>
  <Svg size={size}><path d="M20.54 5.23l-1.39-1.68C18.88 3.21 18.47 3 18 3H6c-.47 0-.88.21-1.16.55L3.46 5.23C3.17 5.57 3 6.02 3 6.5V19c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6.5c0-.48-.17-.93-.46-1.27zM12 17.5L6.5 12H10v-2h4v2h3.5L12 17.5zM5.12 5l.81-1h12l.94 1H5.12z" /></Svg>;
