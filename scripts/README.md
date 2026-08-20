# scripts/ — Ad-hoc debug helpers

These scripts launch a **live Playwright browser** against Trip.com to inspect
scraping behaviour and capture fixture text for offline tests.

| Script | Purpose |
|--------|---------|
| `debug_flight_scrape.py` | Scrape a round-trip flight page and dump the body text to `debug_flight_body.txt` |
| `debug_return_scrape.py` | Click "Select" on an outbound flight, then scrape the return leg list |
| `debug_hotel_image.py` | Open a hotel detail page and inspect available photo URLs |

The `.txt` files in this folder are captured page bodies used as fixtures by
`tests/test_flight_row_parse.py`.  They are **not** committed (listed in
`.gitignore`) since they change with every scrape run.

## Running

```bash
python scripts/debug_flight_scrape.py
python scripts/debug_return_scrape.py
python scripts/debug_hotel_image.py
```

All scripts set `HEADLESS=true` by default. Edit the script or set
`HEADLESS=false` to watch what Playwright is doing.
