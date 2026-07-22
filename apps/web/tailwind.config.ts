import type { Config } from "tailwindcss";
import { relayPreset } from "@relay/shared/tailwind-preset";
import animate from "tailwindcss-animate";

const config: Config = {
  // Shared semantic colors/radii come from the workspace preset (single source of truth).
  presets: [relayPreset as unknown as Partial<Config>],
  content: [
    "./src/app/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
    "./src/lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)"],
      },
    },
  },
  plugins: [animate],
};

export default config;
