import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: [
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      colors: {
        profit: {
          DEFAULT: "#10b981",
          light: "#34d399",
          dark: "#059669",
        },
        loss: {
          DEFAULT: "#ef4444",
          light: "#f87171",
          dark: "#dc2626",
        },
        surface: {
          DEFAULT: "#1e1e2e",
          light: "#2a2a3e",
          dark: "#14141f",
          border: "#3a3a4e",
        },
        accent: {
          DEFAULT: "#6366f1",
          light: "#818cf8",
        },
      },
    },
  },
  plugins: [],
};

export default config;
