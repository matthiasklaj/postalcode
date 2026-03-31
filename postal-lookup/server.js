const express = require('express');
const path = require('path');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

async function nominatimLookup(address, countryCode) {
  const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(address)}&countrycodes=${countryCode.toLowerCase()}&format=json&addressdetails=1&limit=5&accept-language=en`;
  const res = await fetch(url, {
    headers: { 'User-Agent': 'PostalCodeFinder/1.0', 'Accept-Language': 'en' }
  });
  if (!res.ok) throw new Error(`Nominatim ${res.status}`);
  return res.json();
}

async function photonLookup(address, countryCode) {
  const url = `https://photon.komoot.io/api/?q=${encodeURIComponent(address)}&limit=5&lang=en`;
  const res = await fetch(url, { headers: { 'User-Agent': 'PostalCodeFinder/1.0' } });
  if (!res.ok) throw new Error(`Photon ${res.status}`);
  const data = await res.json();
  return (data.features || [])
    .filter(f => (f.properties?.countrycode || '').toUpperCase() === countryCode.toUpperCase())
    .map(f => ({
      postcode: f.properties?.postcode || '',
      city: f.properties?.city || f.properties?.name || '',
      state: f.properties?.state || '',
      country: f.properties?.country || '',
      displayName: [f.properties?.name, f.properties?.street, f.properties?.city, f.properties?.country].filter(Boolean).join(', '),
      lat: f.geometry?.coordinates?.[1],
      lon: f.geometry?.coordinates?.[0],
    }))
    .filter(r => r.postcode);
}

function parseNominatim(items) {
  return items.map(item => {
    const a = item.address || {};
    return {
      postcode: a.postcode || '',
      city: a.city || a.town || a.village || a.hamlet || a.suburb || '',
      state: a.state || a.county || '',
      country: a.country || '',
      displayName: item.display_name || '',
      lat: parseFloat(item.lat),
      lon: parseFloat(item.lon),
    };
  }).filter(r => r.postcode);
}

function dedup(results) {
  const seen = new Set();
  return results.filter(r => { if (seen.has(r.postcode)) return false; seen.add(r.postcode); return true; });
}

app.post('/api/lookup', async (req, res) => {
  const { address, country, countryCode } = req.body;
  if (!address || !countryCode) return res.status(400).json({ error: 'address and countryCode required' });

  let results = [];
  let source = '';

  try {
    const raw = await nominatimLookup(address, countryCode);
    results = parseNominatim(raw);
    if (results.length) source = 'Nominatim (OpenStreetMap)';
  } catch (e) { console.error('Nominatim:', e.message); }

  if (!results.length) {
    try {
      results = await photonLookup(address, countryCode);
      if (results.length) source = 'Photon (Komoot)';
    } catch (e) { console.error('Photon:', e.message); }
  }

  if (!results.length) {
    return res.json({ found: false, reason: 'No results found. Try adding more detail — city name, street number, or a nearby landmark.' });
  }

  const deduped = dedup(results);
  const best = deduped[0];
  res.json({
    found: true,
    postal_code: best.postcode,
    city: best.city,
    state: best.state,
    country: best.country,
    formatted_address: best.displayName,
    lat: best.lat?.toFixed(5),
    lon: best.lon?.toFixed(5),
    source,
    alternatives: deduped.slice(1, 4).map(r => ({
      postal_code: r.postcode,
      area: [r.city, r.state].filter(Boolean).join(', '),
    })),
  });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Postal lookup running on http://localhost:${PORT}`));
