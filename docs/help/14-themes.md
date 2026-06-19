---
title: Themes + visual customisation
nav_app: settings
summary: "Light / dark / system mode; future: per-app themes via the yorik-community marketplace. Pre-alpha state — minimal customisation today."
---

# Themes + visual customisation

## What works today

Settings → Profile → **Theme**: pick **Light**, **Dark**, or **System** (follows OS preference).

That's it for alpha. Yorik ships one design system (Tailwind + shadcn/ui primitives) and the theme toggle only swaps the colour palette.

## What's coming

Two paths on the roadmap:

- **v0.2 — theme variants**: a marketplace of design-token bundles (colours, spacing, typography). Same React components, different look. Install from `yorik-community/themes/`.
- **v0.3 — per-app layouts**: alternative React components for specific apps (calendar variants — minimalist / timeline / week-grid / etc.). Marketplace-pulled, tier-2-reviewed.

Neither is in alpha. The hooks are there (component swap via the view router) but the marketplace is empty.

## If you want to tweak right now

Two unsupported escape hatches:

- **Edit the source**: `frontend-react/src/index.css` carries the CSS variables for the active palette. Change there, run `npm run build`. Survives the bundle's hot reload.
- **User CSS via browser extension**: Stylus or similar, target `localhost:8000`. Doesn't survive the local user but works for personal tweaks.

Both bypass any future migration path. Don't use them if you plan to upgrade Yorik.

## Hint for designers

If you want to contribute a theme post-launch: Yorik uses Tailwind v4 with CSS custom properties. The variables to override are in `frontend-react/src/index.css` (one block per palette). When the theme marketplace lands, contributing a theme will be a single JSON file declaring the variable values + an optional preview screenshot.
