# Postal Code Finder

Find the postal code for any address worldwide — **no API key required**.
Uses OpenStreetMap (Nominatim) with Photon as fallback.

## Setup

```bash
npm install
npm start
```

Open http://localhost:3000 — that's it.

---

## Deploy

### Railway (recommended — free tier)
1. Push to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select the repo — it auto-detects Node.js and deploys

### Render (free tier)
1. Push to GitHub
2. render.com → New Web Service → connect repo
3. Build command: `npm install`
4. Start command: `node server.js`

### Heroku
```bash
heroku create
git push heroku main
```

### Docker
```bash
docker build -t postal-lookup .
docker run -p 3000:3000 postal-lookup
```

---

## How it works

- **No API key needed** — uses free OpenStreetMap geocoding
- Backend (Express) calls Nominatim server-side, avoiding browser CORS issues
- Falls back to Photon (Komoot) if Nominatim returns no results
- Frontend is plain HTML/CSS/JS — no framework, no build step

## Stack
- Node.js + Express (backend)
- Nominatim / Photon (free geocoding APIs)
- Vanilla HTML/CSS/JS (frontend)
