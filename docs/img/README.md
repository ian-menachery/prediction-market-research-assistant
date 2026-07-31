# Screenshots

The root `README.md` embeds three PNGs from this folder — sections of the self-contained study dashboard
(`../index.html`):

| File | Section captured |
| --- | --- |
| `headline-result.png` | Header: the falsifiable hypothesis, kill condition, verdict, and the headline stat tiles. |
| `calibration-curve.png` | The forecasting-calibration hero (small-sample reliability curve + "suggestive, not conclusive / powered study" framing). |
| `strategy-graveyard.png` | The "strategy graveyard" small-multiples grid — every backtested edge, each net-negative after costs. |

## Regenerating them

These are captured straight from `../index.html` (no server, no API spend). With headless Chrome +
Selenium (a dev dependency), render at a 1280px CSS width / 2× device-scale-factor and screenshot the
three sections by element:

- `header.wrap` → `headline-result.png`
- the section containing `#chart-calib` → `calibration-curve.png`
- the section containing `.grave-grid` → `strategy-graveyard.png`

Crop to the full section (no cut-off), trim surrounding whitespace, and keep each PNG reasonably small.
Because `index.html` embeds its data inline, the screenshots always match the live dashboard.
