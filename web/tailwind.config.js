/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        status: {
          open: "#B5472B",
          reported: "#D99A2B",
          claimed_fixed: "#8A8F3A",
          unknown: "#7A7468",
          verified: "#3F7D4E",
          reopened: "#8C1F2D",
        },
      },
    },
  },
  plugins: [],
};
