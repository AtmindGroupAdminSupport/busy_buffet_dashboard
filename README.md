# Buffet Operations Streamlit

The repo contains a Streamlit dashboard that mirrors the metrics described in *Product Description Document.txt*: queue timing, meal durations, table occupancy, walk-away detection, and indoor/outdoor breakdowns. The `old_resources` folder holds the legacy scripts plus the sample Excel/CSV data we used during exploratory work.

## Local setup
1. Activate the workspace virtualenv if it exists: `source .venv/bin/activate` (or recreate it with `python -m venv .venv` and reinstall dependencies).
2. Install the pinned dependencies: `pip install -r requirements.txt`.
3. Provide a Google Sheet connection (see below). The app requires `GOOGLE_SHEET_URL` in secrets or the environment.

## Google Sheet connection
The dashboard reads from a Google Sheet URL. The app now looks for the value in this order:

- `st.secrets["GOOGLE_SHEET_URL"]`
- environment variable `GOOGLE_SHEET_URL`

If neither is set, the app stops with a configuration error instead of exposing a fallback link in the repo.

For local development, create `.streamlit/secrets.toml`:

```toml
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/your-sheet-id/edit?usp=sharing"
```

For Streamlit Community Cloud, open your app settings and add the same key in **Secrets**:

```toml
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/your-sheet-id/edit?usp=sharing"
```

That lets you keep updating and redeploying code from git without storing the sheet link in the repo.

## Running locally
```bash
source .venv/bin/activate
streamlit run app.py --server.port 8501
```
Change the port as needed; Streamlit Cloud or other hosts can reuse the same command.

## Debugging locally
Use the workspace virtualenv so VS Code and the terminal resolve the same packages:

```bash
source .venv/bin/activate
python -m streamlit run app.py --server.port 8501
```

In VS Code, use the included launch configuration to start Streamlit with the `.venv` interpreter.

## Deployment tips
- Commit `requirements.txt` so hosts install the correct versions (`streamlit`, `pandas`, `plotly`, `numpy`, `openpyxl`).
- Configure `GOOGLE_SHEET_URL` in Streamlit Community Cloud Secrets so the live app uses your sheet without storing the link in git.
- Keep `.streamlit/secrets.toml` out of git.
