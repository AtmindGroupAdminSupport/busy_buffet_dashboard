import datetime
import os
import re
import sys
from pathlib import Path

VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != VENV_PYTHON.parent.parent.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__, *sys.argv[1:]])

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.runtime
from streamlit.web import bootstrap
from streamlit_gsheets import GSheetsConnection


PALETTE = {
    "berry": "#92384D",
    "coral": "#DE5F52",
    "orange": "#F08A43",
    "amber": "#FDB447",
    "yellow": "#F9D63A",
    "sand": "#E7D8A1",
}
QUEUING_AREA_TABLE = "99"
DAILY_ANALYTICS_START_TIME = datetime.time(6, 0)
DAILY_ANALYTICS_END_TIME = datetime.time(12, 0)


def get_secret_value(key: str) -> str | None:
    if key in st.secrets:
        value = st.secrets[key]
        return str(value).strip() if value else None
    return None


def resolve_spreadsheet_url() -> str:
    return (
        get_secret_value("GOOGLE_SHEET_URL")
        or os.environ.get("GOOGLE_SHEET_URL", "").strip()
    )


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    red = int(hex_color[0:2], 16)
    green = int(hex_color[2:4], 16)
    blue = int(hex_color[4:6], 16)
    return f"rgba({red}, {green}, {blue}, {alpha})"


def clip_to_daily_analytics_window(
    start_value: pd.Timestamp | datetime.datetime,
    end_value: pd.Timestamp | datetime.datetime,
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    start_timestamp = pd.Timestamp(start_value)
    end_timestamp = pd.Timestamp(end_value)
    if pd.isna(start_timestamp) or pd.isna(end_timestamp) or end_timestamp <= start_timestamp:
        return None

    window_start = pd.Timestamp.combine(start_timestamp.date(), DAILY_ANALYTICS_START_TIME)
    window_end = pd.Timestamp.combine(start_timestamp.date(), DAILY_ANALYTICS_END_TIME)
    clipped_start = max(start_timestamp, window_start)
    clipped_end = min(end_timestamp, window_end)
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end


def build_date_axis(daily: pd.DataFrame) -> dict:
    tick_text = [
        f"<span style='text-decoration:line-through'>{label}</span>" if not has_sheet_data else label
        for label, has_sheet_data in zip(daily["date_label"], daily["has_sheet_data"])
    ]
    return {
        "title": "Date",
        "tickmode": "array",
        "tickvals": daily["date_label"],
        "ticktext": tick_text,
        "ticks": "outside",
    }


def build_count_marker_style(counts: pd.Series, color: str, symbol: str = "circle") -> dict:
    positive_counts = pd.to_numeric(counts, errors="coerce").fillna(0)
    max_count = float(positive_counts.max()) if not positive_counts.empty else 0.0
    min_count = float(positive_counts.min()) if not positive_counts.empty else 0.0

    if max_count > min_count:
        normalized_counts = (positive_counts - min_count) / (max_count - min_count)
    else:
        normalized_counts = pd.Series(1.0 if max_count > 0 else 0.0, index=positive_counts.index, dtype="float64")

    marker_colors = [
        hex_to_rgba(color, 0.35 + (0.6 * float(intensity)))
        for intensity in normalized_counts
    ]
    return {
        "size": 12,
        "color": marker_colors,
        "symbol": symbol,
        "line": {"width": 0},
    }


def mask_series_for_missing_days(values: pd.Series, has_sheet_data: pd.Series) -> list[float | None]:
    numeric_values = pd.to_numeric(pd.Series(values), errors="coerce")
    available_mask = pd.Series(has_sheet_data, index=numeric_values.index).fillna(False).astype(bool)
    return numeric_values.where(available_mask, other=None).tolist()


def find_missing_day_gap_pairs(values: pd.Series, has_sheet_data: pd.Series) -> list[tuple[int, int]]:
    numeric_values = pd.to_numeric(pd.Series(values), errors="coerce")
    available_mask = pd.Series(has_sheet_data, index=numeric_values.index).fillna(False).astype(bool)
    valid_indices = [
        index
        for index, (is_available, value) in enumerate(zip(available_mask.tolist(), numeric_values.tolist()))
        if is_available and pd.notna(value)
    ]

    gap_pairs: list[tuple[int, int]] = []
    for start_index, end_index in zip(valid_indices, valid_indices[1:]):
        if end_index - start_index > 1 and (~available_mask.iloc[start_index + 1 : end_index]).any():
            gap_pairs.append((start_index, end_index))
    return gap_pairs


def extract_sheet_id(spreadsheet_url: str) -> str:
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", spreadsheet_url)
    if not match:
        raise ValueError("Could not find a Google Sheet ID in the provided URL.")
    return match.group(1)


def build_workbook_export_url(spreadsheet_url: str) -> str:
    sheet_id = extract_sheet_id(spreadsheet_url)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"


def parse_sheet_date(sheet_name: str, default_year: int | None = None) -> pd.Timestamp | None:
    name = str(sheet_name).strip()
    if name.isdigit() and len(name) >= 2:
        # Tabs are named as day+month without separators (e.g. 133 -> 13/3, 14 -> 1/4).
        day = int(name[:-1])
        month = int(name[-1])
        if 1 <= day <= 31 and 1 <= month <= 12:
            year = default_year or pd.Timestamp.now().year
            return pd.Timestamp(year, month, day).normalize()
    return None


def expand_table_no(table_no: str) -> list[str]:
    value = str(table_no).strip()
    if not value or value.lower() in {"nan", "nat"}:
        return []
    if "-" not in value:
        return [value]

    parts = [part.strip() for part in value.split("-", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return [value]
    if parts[0].isdigit() and parts[1].isdigit():
        low, high = int(parts[0]), int(parts[1])
        if low <= high:
            return [str(number) for number in range(low, high + 1)]
    return parts


def table_sort_key(name: str) -> tuple[int, int, str]:
    value = str(name).strip()
    match = re.match(r"^(\d+)([A-Za-z]*)$", value)
    if not match:
        return (9999, 999, value)
    number = int(match.group(1))
    suffix = (match.group(2) or "").upper()
    suffix_rank = {"": 0, "A": 1, "B": 2, "C": 3}.get(suffix, 99)
    return (number, suffix_rank, value)


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed:")].copy()

    rename = {
        "service_no.": "service_no",
        "table_no.": "table_no",
        "Guest_type": "guest_type",
        "Date": "date",
    }
    df = df.rename(columns={key: value for key, value in rename.items() if key in df.columns})

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    else:
        df["date"] = pd.NaT

    for column in ["table_no", "guest_type"]:
        if column in df.columns:
            df[column] = df[column].astype(str).str.strip()

    if "pax" in df.columns:
        df["pax"] = pd.to_numeric(df["pax"], errors="coerce").fillna(0)
    else:
        df["pax"] = 0

    time_columns = ["queue_start", "queue_end", "meal_start", "meal_end"]
    base_date = pd.Timestamp("1899-12-30")
    for column in time_columns:
        if column not in df.columns:
            continue

        parsed_values = pd.Series(index=df.index, dtype="datetime64[ns]")
        for index, value in df[column].items():
            if pd.isna(value):
                parsed_values.loc[index] = pd.NaT
                continue
            if isinstance(value, (pd.Timestamp, datetime.datetime)):
                parsed_values.loc[index] = pd.Timestamp.combine(pd.Timestamp.now().date(), value.time())
                continue
            if isinstance(value, (int, float)):
                parsed_values.loc[index] = pd.Timestamp.combine(
                    pd.Timestamp.now().date(),
                    (base_date + pd.Timedelta(days=float(value))).time(),
                )
                continue

            parsed = pd.to_datetime(str(value).strip(), format="%H:%M", errors="coerce")
            if pd.isna(parsed):
                parsed = pd.to_datetime(str(value).strip(), format="%H:%M:%S", errors="coerce")
            if pd.isna(parsed):
                parsed = pd.to_datetime(str(value).strip(), errors="coerce")

            if pd.notna(parsed):
                parsed_values.loc[index] = pd.Timestamp.combine(pd.Timestamp.now().date(), parsed.time())
            else:
                parsed_values.loc[index] = pd.NaT

        df[column] = parsed_values

    has_queue = df["queue_start"].notna() & df["queue_end"].notna()
    has_meal = df["meal_start"].notna() & df["meal_end"].notna()

    df["queue_wait_minutes"] = 0.0
    df.loc[has_queue, "queue_wait_minutes"] = (
        (df.loc[has_queue, "queue_end"] - df.loc[has_queue, "queue_start"]).dt.total_seconds() / 60
    )
    df["walk_away"] = has_queue & ~has_meal
    df["meal_duration_minutes"] = (df["meal_end"] - df["meal_start"]).dt.total_seconds() / 60

    return df


@st.cache_data(show_spinner=False, ttl=300)
def load_google_sheet_workbook(spreadsheet_url: str) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    workbook = pd.ExcelFile(build_workbook_export_url(spreadsheet_url), engine="openpyxl")
    year = pd.Timestamp.now().year
    frames: list[pd.DataFrame] = []
    sheet_dates: list[pd.Timestamp] = []

    for sheet_name in workbook.sheet_names:
        sheet_df = pd.read_excel(workbook, sheet_name=sheet_name, engine="openpyxl")
        parsed_date = parse_sheet_date(sheet_name, default_year=year)
        if parsed_date is not None:
            sheet_dates.append(parsed_date)
            sheet_df["date"] = parsed_date
        elif "Date" not in sheet_df.columns and "date" not in sheet_df.columns:
            sheet_df["date"] = pd.NaT
        frames.append(sheet_df)

    if not frames:
        return pd.DataFrame(), sheet_dates

    combined = pd.concat(frames, ignore_index=True)
    return normalize_dataframe(combined), sheet_dates


def build_daily_summary(df: pd.DataFrame, sheet_dates: list[pd.Timestamp]) -> pd.DataFrame:
    grouped = (
        df.dropna(subset=["date"])
        .groupby("date", as_index=False)
        .agg(
            group_count=("date", "size"),
            walk_in_pax=(
                "pax",
                lambda values: int(
                    pd.to_numeric(values[df.loc[values.index, "guest_type"].astype(str).str.strip().eq("Walk in")], errors="coerce")
                    .fillna(0)
                    .sum()
                ),
            ),
            in_house_pax=(
                "pax",
                lambda values: int(
                    pd.to_numeric(values[df.loc[values.index, "guest_type"].astype(str).str.strip().eq("In house")], errors="coerce")
                    .fillna(0)
                    .sum()
                ),
            ),
            waited_groups=("queue_wait_minutes", lambda values: int((values > 0).sum())),
            avg_wait_minutes=(
                "queue_wait_minutes",
                lambda values: round(values[values > 0].mean(), 1) if (values > 0).any() else 0.0,
            ),
            walk_aways=("walk_away", lambda values: int(values.fillna(False).sum())),
            guest_count=("pax", lambda values: int(pd.to_numeric(values, errors="coerce").fillna(0).sum())),
        )
        .sort_values("date")
    )

    observed_dates = grouped["date"].dropna().dt.normalize().drop_duplicates().sort_values()

    if sheet_dates:
        normalized_sheet_dates = pd.to_datetime(pd.Series(sheet_dates)).dt.normalize().dropna().drop_duplicates().sort_values()
        all_known_dates = pd.concat([observed_dates, normalized_sheet_dates]).dropna().drop_duplicates().sort_values()
        full_dates = pd.DataFrame({"date": pd.date_range(all_known_dates.min(), all_known_dates.max(), freq="D")})
        daily = full_dates.merge(grouped, on="date", how="left")
        daily["has_sheet_data"] = daily["date"].isin(set(observed_dates.tolist()))
    else:
        daily = grouped.copy()
        daily["has_sheet_data"] = daily["date"].isin(set(observed_dates.tolist()))

    for column in [
        "group_count",
        "walk_in_pax",
        "in_house_pax",
        "waited_groups",
        "avg_wait_minutes",
        "walk_aways",
        "guest_count",
    ]:
        daily[column] = daily[column].fillna(0)

    daily["group_count"] = daily["group_count"].astype(int)
    daily["walk_in_pax"] = daily["walk_in_pax"].astype(int)
    daily["in_house_pax"] = daily["in_house_pax"].astype(int)
    daily["waited_groups"] = daily["waited_groups"].astype(int)
    daily["walk_aways"] = daily["walk_aways"].astype(int)
    daily["guest_count"] = daily["guest_count"].astype(int)

    peak_queue_times = []
    for day in daily["date"]:
        day_queues = df[
            (df["date"] == day)
            & df["queue_start"].notna()
            & df["queue_end"].notna()
            & (df["queue_end"] >= df["queue_start"])
        ][["queue_start", "queue_end"]]

        if day_queues.empty:
            peak_queue_times.append("")
            continue

        events: list[tuple[pd.Timestamp, int]] = []
        for queue_start, queue_end in day_queues.itertuples(index=False):
            events.append((queue_start, 1))
            events.append((queue_end, -1))

        events.sort(key=lambda event: (event[0], -event[1]))
        active_queues = 0
        max_queues = 0
        peak_time: pd.Timestamp | None = None

        for timestamp, delta in events:
            active_queues += delta
            if active_queues > max_queues:
                max_queues = active_queues
                peak_time = timestamp

        peak_queue_times.append("" if peak_time is None or max_queues == 0 else peak_time.strftime("%H:%M"))

    daily["peak_queue_time"] = peak_queue_times
    daily["date_label"] = daily["date"].dt.strftime("%Y-%m-%d (%A)")
    daily["day_type"] = daily["date"].dt.dayofweek.map(lambda value: "Weekend" if value >= 5 else "Weekday")
    daily["walkaway_delta"] = daily["walk_aways"].diff().fillna(daily["walk_aways"]).astype(int)
    return daily


def format_metric_delta(value: float, *, comparison_label: str, suffix: str = "", decimals: int = 0) -> str:
    formatted_value = f"{value:+.{decimals}f}"
    if decimals == 0:
        formatted_value = f"{int(round(value)):+d}"
    return f"{formatted_value}{suffix} vs {comparison_label}"


def build_wait_walkaway_chart(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    walkaway_labels = daily["walk_aways"].apply(lambda value: "" if value == 0 else str(int(value)))
    walkaway_point_mask = daily["walk_aways"].notna() & daily["has_sheet_data"].fillna(False)
    walkaway_line = mask_series_for_missing_days(daily["walk_aways"], daily["has_sheet_data"])
    walkaway_gap_pairs = find_missing_day_gap_pairs(daily["walk_aways"], daily["has_sheet_data"])
    fig.add_scatter(
        x=daily["date_label"],
        y=walkaway_line,
        name="Walk-aways",
        mode="lines",
        yaxis="y2",
        line=dict(color=hex_to_rgba(PALETTE["berry"], 0.8), width=2),
        customdata=daily[["waited_groups"]],
        hovertemplate=(
            "Date: %{x}<br>Walk-aways: %{y}"
            "<br>Groups that waited: %{customdata[0]}<extra></extra>"
        ),
        legendrank=2,
    )
    if walkaway_gap_pairs:
        fig.add_scatter(
            x=[item for start, end in walkaway_gap_pairs for item in (daily.iloc[start]["date_label"], daily.iloc[end]["date_label"], None)],
            y=[item for start, end in walkaway_gap_pairs for item in (daily.iloc[start]["walk_aways"], daily.iloc[end]["walk_aways"], None)],
            name="Walk-aways gap",
            mode="lines",
            yaxis="y2",
            line=dict(color=hex_to_rgba(PALETTE["berry"], 0.8), width=2, dash="dot"),
            hoverinfo="skip",
            showlegend=False,
        )
    fig.add_bar(
        x=daily["date_label"],
        y=daily["avg_wait_minutes"],
        name="Average wait (minutes)",
        marker_color=PALETTE["orange"],
        opacity=0.9,
        text=daily.apply(
            lambda row: row["peak_queue_time"] if row["avg_wait_minutes"] > 0 and row["peak_queue_time"] else "",
            axis=1,
        ),
        textposition="outside",
        hovertemplate="Date: %{x}<br>Average wait: %{y:.1f} min<extra></extra>",
        legendrank=1,
    )
    fig.add_scatter(
        x=daily.loc[walkaway_point_mask, "date_label"],
        y=daily.loc[walkaway_point_mask, "walk_aways"],
        name="Walk-aways",
        mode="markers+text",
        text=walkaway_labels[walkaway_point_mask],
        textposition="top center",
        yaxis="y2",
        marker=build_count_marker_style(daily.loc[walkaway_point_mask, "walk_aways"], PALETTE["berry"]),
        customdata=daily.loc[walkaway_point_mask, ["waited_groups"]],
        hovertemplate=(
            "Date: %{x}<br>Walk-aways: %{y}"
            "<br>Groups that waited: %{customdata[0]}<extra></extra>"
        ),
        showlegend=False,
    )
    fig.update_layout(
        title="Wait pressure and walk-aways by date",
        xaxis=dict(title="Date", tickmode="linear"),
        yaxis=dict(title="Average wait (minutes)", rangemode="tozero"),
        yaxis2=dict(
            title="Walk-aways",
            overlaying="y",
            side="right",
            rangemode="tozero",
            showline=False,
            showgrid=False,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        template="plotly_white",
        annotations=[
            dict(
                x=row["date_label"],
                y=row["walk_aways"],
                xref="x",
                yref="y2",
                text=(
                    f"High walk-away: {int(row['walk_aways'])}<br>"
                    f"Δ{row['walkaway_delta']:+d}"
                ),
                showarrow=True,
                arrowhead=1,
                ay=-30,
                bgcolor="white",
                bordercolor=PALETTE["berry"],
                font=dict(size=10, color=PALETTE["berry"]),
            )
            for _, row in daily.nlargest(3, "walk_aways").iterrows()
            if row["walk_aways"] > 0
        ],
    )
    return fig


def build_queue_with_walkaway_area_chart(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    nonzero_walkaways = daily["walk_aways"] > 0
    queue_point_mask = daily["waited_groups"].notna() & daily["has_sheet_data"].fillna(False)
    walkaway_labels = daily["walk_aways"].apply(lambda value: f"WA: {int(value)}" if value > 0 else "")
    queue_line = mask_series_for_missing_days(daily["waited_groups"], daily["has_sheet_data"])
    queue_gap_pairs = find_missing_day_gap_pairs(daily["waited_groups"], daily["has_sheet_data"])
    bar_colors = [
        hex_to_rgba(PALETTE["coral"], 0.78) if walkaways > 0 else hex_to_rgba(PALETTE["berry"], 0.22)
        for walkaways in daily["walk_aways"]
    ]

    fig.add_bar(
        x=daily["date_label"],
        y=daily["avg_wait_minutes"],
        name="Average wait (minutes)",
        marker_color=bar_colors,
        opacity=0.9,
        text=daily.apply(
            lambda row: row["peak_queue_time"] if row["avg_wait_minutes"] > 0 and row["peak_queue_time"] else "",
            axis=1,
        ),
        textposition="outside",
        hovertemplate="Date: %{x}<br>Average wait: %{y:.1f} min<extra></extra>",
        legendrank=1,
    )
    fig.add_scatter(
        x=daily["date_label"],
        y=queue_line,
        name="Number of queues",
        mode="lines",
        yaxis="y2",
        line=dict(color=hex_to_rgba(PALETTE["berry"], 0.55), width=2),
        customdata=daily[["walk_aways"]],
        hovertemplate=(
            "Date: %{x}<br>Number of queues: %{y}"
            "<br>Walk-aways: %{customdata[0]}<extra></extra>"
        ),
        legendrank=2,
    )
    if queue_gap_pairs:
        fig.add_scatter(
            x=[item for start, end in queue_gap_pairs for item in (daily.iloc[start]["date_label"], daily.iloc[end]["date_label"], None)],
            y=[item for start, end in queue_gap_pairs for item in (daily.iloc[start]["waited_groups"], daily.iloc[end]["waited_groups"], None)],
            name="Number of queues gap",
            mode="lines",
            yaxis="y2",
            line=dict(color=hex_to_rgba(PALETTE["berry"], 0.55), width=2, dash="dot"),
            hoverinfo="skip",
            showlegend=False,
        )
    fig.add_scatter(
        x=daily.loc[queue_point_mask, "date_label"],
        y=daily.loc[queue_point_mask, "waited_groups"],
        name="Number of queues markers",
        mode="markers",
        yaxis="y2",
        marker=dict(size=7, color=hex_to_rgba(PALETTE["berry"], 0.55), symbol="x"),
        customdata=daily.loc[queue_point_mask, ["walk_aways"]],
        hovertemplate=(
            "Date: %{x}<br>Number of queues: %{y}"
            "<br>Walk-aways: %{customdata[0]}<extra></extra>"
        ),
        showlegend=False,
    )
    fig.add_scatter(
        x=daily.loc[nonzero_walkaways, "date_label"],
        y=daily.loc[nonzero_walkaways, "walk_aways"],
        name="Walk-away labels",
        mode="markers+text",
        text=walkaway_labels[nonzero_walkaways],
        textposition="top center",
        yaxis="y2",
        marker=build_count_marker_style(daily.loc[nonzero_walkaways, "walk_aways"], PALETTE["berry"], symbol="x"),
        hovertemplate="Date: %{x}<br>Walk-aways: %{y}<extra></extra>",
        showlegend=False,
    )
    fig.update_layout(
        title="Queue pressure and walk-aways by date",
        xaxis=build_date_axis(daily),
        yaxis=dict(title="Average wait (minutes)", rangemode="tozero", ticks="outside"),
        yaxis2=dict(
            title="Count",
            overlaying="y",
            side="right",
            rangemode="tozero",
            showline=False,
            showgrid=False,
            ticks="outside",
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        template="plotly_white",
    )
    return fig


def build_guest_count_and_queue_chart(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_bar(
        x=daily["date_label"],
        y=daily["walk_in_pax"],
        name="Walk in (pax)",
        marker_color=hex_to_rgba(PALETTE["amber"], 0.82),
        customdata=daily[["in_house_pax", "waited_groups"]],
        hovertemplate=(
            "Date: %{x}<br>Walk in (pax): %{y}"
            "<br>In house (pax): %{customdata[0]}"
            "<br>Number of queues: %{customdata[1]}<extra></extra>"
        ),
    )
    fig.add_bar(
        x=daily["date_label"],
        y=daily["in_house_pax"],
        name="In house (pax)",
        marker_color=hex_to_rgba(PALETTE["coral"], 0.82),
        customdata=daily[["walk_in_pax", "waited_groups"]],
        hovertemplate=(
            "Date: %{x}<br>In house (pax): %{y}"
            "<br>Walk in (pax): %{customdata[0]}"
            "<br>Number of queues: %{customdata[1]}<extra></extra>"
        ),
    )
    nonzero_queue_mask = daily["waited_groups"] > 0
    fig.add_scatter(
        x=daily.loc[nonzero_queue_mask, "date_label"],
        y=daily.loc[nonzero_queue_mask, "waited_groups"],
        name="Number of queues",
        yaxis="y2",
        mode="markers",
        marker=dict(
            size=10,
            color=daily.loc[nonzero_queue_mask, "avg_wait_minutes"],
            colorscale=[[0, hex_to_rgba(PALETTE["berry"], 0.45)], [1, PALETTE["berry"]]],
            showscale=False,
            line=dict(width=0),
        ),
        customdata=daily.loc[nonzero_queue_mask, ["walk_in_pax", "in_house_pax", "avg_wait_minutes"]],
        hovertemplate=(
            "Date: %{x}<br>Number of queues: %{y}"
            "<br>Average wait (min): %{customdata[2]:.1f}"
            "<br>Walk in (pax): %{customdata[0]}"
            "<br>In house (pax): %{customdata[1]}<extra></extra>"
        ),
    )
    fig.update_layout(
        title="Guest pax split and number of queues by date",
        xaxis=build_date_axis(daily),
        yaxis=dict(title="Count", rangemode="tozero", ticks="outside"),
        yaxis2=dict(
            title="Number of queues",
            overlaying="y",
            side="right",
            rangemode="tozero",
            showline=False,
            showgrid=False,
            ticks="outside",
        ),
        barmode="stack",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        template="plotly_white",
    )
    return fig


def build_daily_seating_gantt(day_df: pd.DataFrame) -> go.Figure:
    seating_rows: list[dict[str, object]] = []
    for row in day_df.itertuples(index=False):
        if pd.isna(row.meal_start) or pd.isna(row.meal_end):
            continue
        clipped_meal_window = clip_to_daily_analytics_window(row.meal_start, row.meal_end)
        if clipped_meal_window is None:
            continue
        clipped_meal_start, clipped_meal_end = clipped_meal_window
        for table in expand_table_no(row.table_no):
            if table == QUEUING_AREA_TABLE:
                continue
            seating_rows.append(
                {
                    "table_no": table,
                    "meal_start": clipped_meal_start,
                    "meal_end": clipped_meal_end,
                    "guest_type": row.guest_type if getattr(row, "guest_type", "") else "Unknown",
                    "service_no": getattr(row, "service_no", ""),
                    "duration_minutes": max(0.0, (clipped_meal_end - clipped_meal_start).total_seconds() / 60),
                }
            )

    if not seating_rows:
        return go.Figure()

    seating_df = pd.DataFrame(seating_rows)
    table_order = sorted(seating_df["table_no"].unique(), key=table_sort_key)
    fig = px.timeline(
        seating_df,
        x_start="meal_start",
        x_end="meal_end",
        y="table_no",
        color="guest_type",
        category_orders={"table_no": table_order},
        color_discrete_map={"In house": PALETTE["coral"], "Walk in": PALETTE["amber"], "Unknown": PALETTE["sand"]},
        custom_data=["meal_end", "guest_type", "service_no", "duration_minutes"],
        title="Customer sitting time by table",
    )
    fig.update_traces(
        hovertemplate=(
            "Table %{y}<br>"
            "Start %{base|%H:%M}<br>"
            "End %{customdata[0]|%H:%M}<br>"
            "Guest type %{customdata[1]}<br>"
            "Service %{customdata[2]}<br>"
            "Sit duration %{customdata[3]:.1f} min<extra></extra>"
        )
    )
    fig.update_yaxes(autorange="reversed", categoryorder="array", categoryarray=table_order)
    fig.update_layout(
        xaxis_title="Time",
        yaxis_title="Table",
        legend_title_text="Guest type",
        template="plotly_white",
        hovermode="closest",
    )
    return fig


def build_daily_load_profile(day_df: pd.DataFrame, *, time_bin_frequency: str = "5min") -> pd.DataFrame:
    occupancy_slices: list[pd.DataFrame] = []
    for row in day_df.itertuples(index=False):
        if pd.isna(row.meal_start) or pd.isna(row.meal_end):
            continue
        clipped_meal_window = clip_to_daily_analytics_window(row.meal_start, row.meal_end)
        if clipped_meal_window is None:
            continue
        clipped_meal_start, clipped_meal_end = clipped_meal_window
        time_index = pd.date_range(
            start=clipped_meal_start.floor(time_bin_frequency),
            end=clipped_meal_end.ceil(time_bin_frequency),
            freq=time_bin_frequency,
            inclusive="left",
        )
        if len(time_index) == 0:
            continue
        for table in expand_table_no(row.table_no):
            if table == QUEUING_AREA_TABLE:
                continue
            occupancy_slices.append(pd.DataFrame({"time": time_index, "table_no": table}))

    queue_slices: list[pd.DataFrame] = []
    for row in day_df[day_df["queue_start"].notna() & day_df["queue_end"].notna()].itertuples(index=False):
        clipped_queue_window = clip_to_daily_analytics_window(row.queue_start, row.queue_end)
        if clipped_queue_window is None:
            continue
        clipped_queue_start, clipped_queue_end = clipped_queue_window
        time_index = pd.date_range(
            start=clipped_queue_start.floor(time_bin_frequency),
            end=clipped_queue_end.ceil(time_bin_frequency),
            freq=time_bin_frequency,
            inclusive="left",
        )
        if len(time_index) == 0:
            continue
        queue_slices.append(pd.DataFrame({"time": time_index}))

    seated = (
        pd.concat(occupancy_slices).groupby("time")["table_no"].nunique().reset_index(name="seated")
        if occupancy_slices
        else pd.DataFrame(columns=["time", "seated"])
    )
    queued = (
        pd.concat(queue_slices).groupby("time").size().reset_index(name="queue")
        if queue_slices
        else pd.DataFrame(columns=["time", "queue"])
    )
    if seated.empty and queued.empty:
        return pd.DataFrame(columns=["time", "seated", "queue", "time_label"])

    start_time = min(frame["time"].min() for frame in [seated, queued] if not frame.empty)
    end_time = max(frame["time"].max() for frame in [seated, queued] if not frame.empty)
    load = pd.DataFrame({"time": pd.date_range(start_time, end_time, freq=time_bin_frequency)})
    load = load.merge(seated, on="time", how="left").merge(queued, on="time", how="left").fillna(0)
    load["time_label"] = load["time"].dt.strftime("%H:%M")
    return load


def build_average_daily_load_profile(comparison_day_df: pd.DataFrame, *, time_bin_frequency: str = "5min") -> pd.DataFrame:
    if comparison_day_df.empty:
        return pd.DataFrame(columns=["time_label", "seated", "queue"])

    daily_profiles: list[pd.DataFrame] = []
    for comparison_date in sorted(comparison_day_df["date"].dropna().dt.normalize().unique()):
        profile = build_daily_load_profile(
            comparison_day_df[comparison_day_df["date"] == comparison_date].copy(),
            time_bin_frequency=time_bin_frequency,
        )
        if profile.empty:
            continue
        daily_profiles.append(profile[["time_label", "seated", "queue"]])

    if not daily_profiles:
        return pd.DataFrame(columns=["time_label", "seated", "queue"])

    combined = pd.concat(daily_profiles, ignore_index=True)
    averaged = combined.groupby("time_label", as_index=False)[["seated", "queue"]].mean()
    return averaged.sort_values("time_label")


def build_daily_load_chart(day_df: pd.DataFrame, comparison_day_df: pd.DataFrame | None = None) -> go.Figure:
    load = build_daily_load_profile(day_df)
    if load.empty:
        return go.Figure()

    comparison_load = build_average_daily_load_profile(comparison_day_df.copy()) if comparison_day_df is not None else pd.DataFrame()

    fig = go.Figure()
    if not comparison_load.empty:
        fig.add_scatter(
            x=comparison_load["time_label"],
            y=comparison_load["seated"],
            name="Average seated tables",
            mode="lines",
            line=dict(color=hex_to_rgba(PALETTE["orange"], 0.3), width=0),
            fill="tozeroy",
            fillcolor=hex_to_rgba(PALETTE["orange"], 0.14),
            hovertemplate="Time: %{x}<br>Average seated tables: %{y:.1f}<extra></extra>",
        )
        fig.add_scatter(
            x=comparison_load["time_label"],
            y=comparison_load["seated"],
            name="Average seated tables outline",
            mode="lines",
            line=dict(color=hex_to_rgba(PALETTE["orange"], 0.55), width=2, dash="dot"),
            hovertemplate="Time: %{x}<br>Average seated tables: %{y:.1f}<extra></extra>",
            showlegend=False,
        )
        fig.add_scatter(
            x=comparison_load["time_label"],
            y=comparison_load["queue"],
            name="Average waiting groups",
            mode="lines",
            line=dict(color=hex_to_rgba(PALETTE["berry"], 0.3), width=0),
            fill="tozeroy",
            fillcolor=hex_to_rgba(PALETTE["berry"], 0.12),
            hovertemplate="Time: %{x}<br>Average waiting groups: %{y:.1f}<extra></extra>",
        )
        fig.add_scatter(
            x=comparison_load["time_label"],
            y=comparison_load["queue"],
            name="Average waiting groups outline",
            mode="lines",
            line=dict(color=hex_to_rgba(PALETTE["berry"], 0.6), width=2, dash="dot"),
            hovertemplate="Time: %{x}<br>Average waiting groups: %{y:.1f}<extra></extra>",
            showlegend=False,
        )

    fig.add_bar(x=load["time_label"], y=load["seated"], name="Seated tables", marker_color=PALETTE["orange"])
    fig.add_bar(x=load["time_label"], y=load["queue"], name="Waiting groups", marker_color=PALETTE["berry"])
    fig.update_layout(
        title="Stacked seated and waiting load by time of day",
        xaxis_title="Time",
        yaxis_title="Count",
        barmode="stack",
        template="plotly_white",
        hovermode="x unified",
    )
    return fig


def build_daily_occupancy_profile(day_df: pd.DataFrame, *, time_bin_frequency: str = "5min") -> pd.DataFrame:
    occupancy_slices: list[pd.DataFrame] = []
    for row in day_df.itertuples(index=False):
        if pd.isna(row.meal_start) or pd.isna(row.meal_end):
            continue
        clipped_meal_window = clip_to_daily_analytics_window(row.meal_start, row.meal_end)
        if clipped_meal_window is None:
            continue
        clipped_meal_start, clipped_meal_end = clipped_meal_window
        time_index = pd.date_range(
            start=clipped_meal_start.floor(time_bin_frequency),
            end=clipped_meal_end.ceil(time_bin_frequency),
            freq=time_bin_frequency,
            inclusive="left",
        )
        if len(time_index) == 0:
            continue
        for table in expand_table_no(row.table_no):
            if table == QUEUING_AREA_TABLE:
                continue
            occupancy_slices.append(
                pd.DataFrame(
                    {
                        "time": time_index,
                        "table_no": table,
                        "guest_type": row.guest_type if getattr(row, "guest_type", "") else "Unknown",
                    }
                )
            )

    if not occupancy_slices:
        return pd.DataFrame(columns=["time", "table_no", "guest_type", "tables_occupied", "time_label"])

    occupancy_df = pd.concat(occupancy_slices)
    occupancy_by_guest_type = (
        occupancy_df.groupby(["time", "guest_type"])["table_no"]
        .nunique()
        .reset_index(name="tables_occupied")
    )
    occupancy_by_guest_type["time_label"] = occupancy_by_guest_type["time"].dt.strftime("%H:%M")
    return occupancy_by_guest_type


def build_average_daily_occupancy_profile(
    comparison_day_df: pd.DataFrame,
    *,
    time_bin_frequency: str = "5min",
) -> pd.DataFrame:
    if comparison_day_df.empty:
        return pd.DataFrame(columns=["time_label", "guest_type", "tables_occupied"])

    daily_profiles: list[pd.DataFrame] = []
    for comparison_date in sorted(comparison_day_df["date"].dropna().dt.normalize().unique()):
        profile = build_daily_occupancy_profile(
            comparison_day_df[comparison_day_df["date"] == comparison_date].copy(),
            time_bin_frequency=time_bin_frequency,
        )
        if profile.empty:
            continue
        daily_profiles.append(profile[["time_label", "guest_type", "tables_occupied"]])

    if not daily_profiles:
        return pd.DataFrame(columns=["time_label", "guest_type", "tables_occupied"])

    combined = pd.concat(daily_profiles, ignore_index=True)
    averaged = combined.groupby(["time_label", "guest_type"], as_index=False)["tables_occupied"].mean()
    return averaged.sort_values(["time_label", "guest_type"])


def build_daily_occupancy_chart(day_df: pd.DataFrame, comparison_day_df: pd.DataFrame | None = None) -> go.Figure:
    occupancy_by_guest_type = build_daily_occupancy_profile(day_df)
    if occupancy_by_guest_type.empty:
        return go.Figure()

    comparison_occupancy = (
        build_average_daily_occupancy_profile(comparison_day_df.copy())
        if comparison_day_df is not None
        else pd.DataFrame()
    )
    fig = go.Figure()
    color_discrete_map = {"In house": PALETTE["coral"], "Walk in": PALETTE["amber"], "Unknown": PALETTE["sand"]}
    guest_type_order = ["In house", "Walk in", "Unknown"]

    if not comparison_occupancy.empty:
        for guest_type in guest_type_order:
            series = comparison_occupancy[comparison_occupancy["guest_type"] == guest_type]
            if series.empty:
                continue
            fig.add_scatter(
                x=series["time_label"],
                y=series["tables_occupied"],
                name=f"Average {guest_type}",
                mode="lines",
                line=dict(color=hex_to_rgba(color_discrete_map[guest_type], 0.3), width=0),
                fill="tozeroy",
                fillcolor=hex_to_rgba(color_discrete_map[guest_type], 0.12),
                hovertemplate=(
                    f"Time: %{{x}}<br>Average {guest_type} tables: %{{y:.1f}}<extra></extra>"
                ),
            )
            fig.add_scatter(
                x=series["time_label"],
                y=series["tables_occupied"],
                name=f"Average {guest_type} outline",
                mode="lines",
                line=dict(color=hex_to_rgba(color_discrete_map[guest_type], 0.6), width=2, dash="dot"),
                hovertemplate=(
                    f"Time: %{{x}}<br>Average {guest_type} tables: %{{y:.1f}}<extra></extra>"
                ),
                showlegend=False,
            )

    for guest_type in guest_type_order:
        series = occupancy_by_guest_type[occupancy_by_guest_type["guest_type"] == guest_type]
        if series.empty:
            continue
        fig.add_bar(
            x=series["time_label"],
            y=series["tables_occupied"],
            name=guest_type,
            marker_color=color_discrete_map[guest_type],
            hovertemplate=(
                f"Time: %{{x}}<br>{guest_type} tables: %{{y}}<extra></extra>"
            ),
        )

    fig.update_layout(template="plotly_white", hovermode="x unified")
    fig.update_layout(
        title="Table occupancy by time of day",
        xaxis_title="Time",
        yaxis_title="Tables occupied",
        barmode="stack",
    )
    return fig


def render_plotly_chart(fig: go.Figure, *, key: str | None = None) -> None:
    st.plotly_chart(fig, use_container_width=True, theme=None, key=key)


def main() -> None:
    st.set_page_config(page_title="Buffet Analytics", layout="wide")
    st.title("Buffet Analytics")
    spreadsheet_url = resolve_spreadsheet_url()
    if not spreadsheet_url:
        st.error("Set GOOGLE_SHEET_URL in Streamlit secrets or as an environment variable before running the app.")
        st.stop()

    connection = st.connection("gsheets", type=GSheetsConnection)

    try:
        connection.read(spreadsheet=spreadsheet_url, ttl=300)
    except Exception as exc:
        st.error(f"Could not connect to the public Google Sheet: {exc}")
        st.stop()

    try:
        df, sheet_dates = load_google_sheet_workbook(spreadsheet_url)
    except Exception as exc:
        st.error(f"Could not load the workbook export from Google Sheets: {exc}")
        st.stop()

    daily = build_daily_summary(df, sheet_dates)
    if daily.empty:
        st.warning("No dated records were found in the workbook.")
        st.stop()

    overview_tab, daily_tab = st.tabs(["Overview", "Daily Analytics"])

    with overview_tab:
        overview_min_date = daily["date"].min().date()
        overview_max_date = daily["date"].max().date()
        date_filter_left, date_filter_right = st.columns(2)
        overview_start_date = date_filter_left.date_input(
            "Start date",
            value=overview_min_date,
            min_value=overview_min_date,
            max_value=overview_max_date,
            key="overview_start_date",
        )
        overview_end_date = date_filter_right.date_input(
            "End date",
            value=overview_max_date,
            min_value=overview_min_date,
            max_value=overview_max_date,
            key="overview_end_date",
        )

        if overview_start_date > overview_end_date:
            st.warning("Start date must be on or before end date.")
            st.stop()

        filtered_daily = daily[
            daily["date"].between(pd.Timestamp(overview_start_date), pd.Timestamp(overview_end_date))
        ].copy()

        render_plotly_chart(build_queue_with_walkaway_area_chart(filtered_daily))
        render_plotly_chart(build_guest_count_and_queue_chart(filtered_daily))
        st.markdown("*WA = walk away | Time on bar is peak of queue waiting*")

        peak_wait_row = filtered_daily.loc[filtered_daily["avg_wait_minutes"].idxmax()]
        peak_walkaway_row = filtered_daily.loc[filtered_daily["walk_aways"].idxmax()]
        metric_left, metric_right = st.columns(2)
        metric_left.metric(
            "Highest average wait",
            f"{peak_wait_row['avg_wait_minutes']:.1f} min",
            peak_wait_row["date_label"],
        )
        metric_right.metric(
            "Most walk-aways",
            int(peak_walkaway_row["walk_aways"]),
            peak_walkaway_row["date_label"],
        )

        with st.expander("Daily summary data"):
            st.dataframe(
                filtered_daily[["date_label", "guest_count", "waited_groups", "avg_wait_minutes", "walk_aways"]],
                use_container_width=True,
            )
            unique_sheet_dates = (
                pd.to_datetime(pd.Series(sheet_dates)).dt.normalize().dropna().drop_duplicates().sort_values()
            )
            if not unique_sheet_dates.empty:
                st.caption(
                    "Google Sheets dates pulled: "
                    f"{len(unique_sheet_dates)} "
                    f"({unique_sheet_dates.min().strftime('%Y-%m-%d')} to "
                    f"{unique_sheet_dates.max().strftime('%Y-%m-%d')})"
                )
            else:
                st.caption("Google Sheets dates pulled: 0")

    with daily_tab:
        available_dates = sorted(df["date"].dropna().dt.normalize().unique())
        if not available_dates:
            st.info("No daily records are available for detailed analytics.")
            return

        selector_left, selector_right = st.columns(2)
        selected_date = selector_left.selectbox(
            "Choose a day",
            options=available_dates,
            index=len(available_dates) - 1,
            format_func=lambda value: pd.Timestamp(value).strftime("%Y-%m-%d"),
            key="daily_selected_date",
        )
        selected_day = pd.Timestamp(selected_date).normalize()
        default_comparison_mode = "weekend (average)" if selected_day.dayofweek >= 5 else "weekday (average)"
        if (
            st.session_state.get("daily_compare_anchor_date") != selected_day
            or "daily_comparison_mode" not in st.session_state
        ):
            st.session_state["daily_comparison_mode"] = default_comparison_mode
            st.session_state["daily_compare_anchor_date"] = selected_day
        comparison_mode = selector_right.selectbox(
            "Choose compare",
            options=[
                "weekday (average)",
                "weekend (average)",
                "same day of week (average)",
            ],
            key="daily_comparison_mode",
        )
        day_df = df[df["date"] == pd.Timestamp(selected_date).normalize()].copy()
        selected_day_summary = daily.loc[daily["date"] == selected_day].iloc[0]

        if comparison_mode == "weekday (average)":
            comparison_daily = daily[daily["has_sheet_data"] & daily["date"].dt.dayofweek.lt(5)].copy()
            comparison_label = "weekday"
        elif comparison_mode == "weekend (average)":
            comparison_daily = daily[daily["has_sheet_data"] & daily["date"].dt.dayofweek.ge(5)].copy()
            comparison_label = "weekend"
        else:
            selected_weekday = int(selected_day.dayofweek)
            comparison_daily = daily[daily["has_sheet_data"] & daily["date"].dt.dayofweek.eq(selected_weekday)].copy()
            comparison_label = selected_day.day_name().lower()

        average_group_count = comparison_daily["group_count"].mean() if not comparison_daily.empty else 0.0
        average_guest_count = comparison_daily["guest_count"].mean() if not comparison_daily.empty else 0.0
        average_walk_in_pax = comparison_daily["walk_in_pax"].mean() if not comparison_daily.empty else 0.0
        average_in_house_pax = comparison_daily["in_house_pax"].mean() if not comparison_daily.empty else 0.0
        comparison_day_df = df[df["date"].isin(comparison_daily["date"])].copy()
        comparison_waits = pd.to_numeric(comparison_day_df["queue_wait_minutes"], errors="coerce")
        positive_comparison_waits = comparison_waits[comparison_waits > 0]
        comparison_longest_wait_minutes = float(positive_comparison_waits.max()) if not positive_comparison_waits.empty else 0.0
        comparison_average_wait_minutes = float(positive_comparison_waits.mean()) if not positive_comparison_waits.empty else 0.0
        comparison_median_wait_minutes = float(positive_comparison_waits.median()) if not positive_comparison_waits.empty else 0.0
        comparison_walk_aways = comparison_daily["walk_aways"].mean() if not comparison_daily.empty else 0.0
        comparison_max_queue_groups: list[int] = []
        comparison_queue_appearance_hours: list[float] = []
        for comparison_date in comparison_daily["date"]:
            comparison_queues = comparison_day_df[
                (comparison_day_df["date"] == comparison_date)
                & comparison_day_df["queue_start"].notna()
                & comparison_day_df["queue_end"].notna()
                & (comparison_day_df["queue_end"] >= comparison_day_df["queue_start"])
            ][["queue_start", "queue_end"]]
            comparison_longest_queue_group_count = 0
            comparison_queue_duration_minutes = 0.0
            if not comparison_queues.empty:
                comparison_queue_events: list[tuple[pd.Timestamp, int]] = []
                for queue_start, queue_end in comparison_queues.itertuples(index=False):
                    comparison_queue_events.append((queue_start, 1))
                    comparison_queue_events.append((queue_end, -1))

                comparison_queue_events.sort(key=lambda event: (event[0], -event[1]))
                comparison_active_queue_groups = 0
                comparison_current_queue_start: pd.Timestamp | None = None
                for event_time, delta in comparison_queue_events:
                    previous_active_queue_groups = comparison_active_queue_groups
                    comparison_active_queue_groups += delta
                    if previous_active_queue_groups == 0 and comparison_active_queue_groups > 0:
                        comparison_current_queue_start = event_time
                    elif (
                        previous_active_queue_groups > 0
                        and comparison_active_queue_groups == 0
                        and comparison_current_queue_start is not None
                    ):
                        comparison_queue_duration_minutes += (
                            event_time - comparison_current_queue_start
                        ).total_seconds() / 60
                        comparison_current_queue_start = None
                    comparison_longest_queue_group_count = max(
                        comparison_longest_queue_group_count,
                        comparison_active_queue_groups,
                    )

            comparison_max_queue_groups.append(comparison_longest_queue_group_count)
            comparison_queue_appearance_hours.append(comparison_queue_duration_minutes / 60)

        comparison_max_queue_group_count = (
            sum(comparison_max_queue_groups) / len(comparison_max_queue_groups)
            if comparison_max_queue_groups
            else 0.0
        )
        comparison_queue_appearance_hours_value = (
            sum(comparison_queue_appearance_hours) / len(comparison_queue_appearance_hours)
            if comparison_queue_appearance_hours
            else 0.0
        )
        waited_values = pd.to_numeric(day_df["queue_wait_minutes"], errors="coerce")
        positive_waited_values = waited_values[waited_values > 0]
        longest_wait_minutes = float(positive_waited_values.max()) if not positive_waited_values.empty else 0.0
        average_wait_minutes = float(positive_waited_values.mean()) if not positive_waited_values.empty else 0.0
        median_wait_minutes = float(positive_waited_values.median()) if not positive_waited_values.empty else 0.0
        day_queues = day_df[
            day_df["queue_start"].notna()
            & day_df["queue_end"].notna()
            & (day_df["queue_end"] >= day_df["queue_start"])
        ][["queue_start", "queue_end"]]
        longest_queue_group_count = 0
        queue_duration_minutes = 0.0
        if not day_queues.empty:
            queue_events: list[tuple[pd.Timestamp, int]] = []
            for queue_start, queue_end in day_queues.itertuples(index=False):
                queue_events.append((queue_start, 1))
                queue_events.append((queue_end, -1))

            queue_events.sort(key=lambda event: (event[0], -event[1]))
            active_queue_groups = 0
            current_queue_start: pd.Timestamp | None = None
            for _, delta in queue_events:
                previous_active_queue_groups = active_queue_groups
                active_queue_groups += delta
                event_time = _
                if previous_active_queue_groups == 0 and active_queue_groups > 0:
                    current_queue_start = event_time
                elif previous_active_queue_groups > 0 and active_queue_groups == 0 and current_queue_start is not None:
                    queue_duration_minutes += (event_time - current_queue_start).total_seconds() / 60
                    current_queue_start = None
                longest_queue_group_count = max(longest_queue_group_count, active_queue_groups)
        queue_hours = queue_duration_minutes / 60
        if pd.isna(longest_wait_minutes):
            longest_wait_minutes = 0.0
        if pd.isna(average_wait_minutes):
            average_wait_minutes = 0.0
        if pd.isna(median_wait_minutes):
            median_wait_minutes = 0.0

        st.caption(f"{selected_day_summary['date_label']} metrics.")
        st.markdown(
            f"Comparison deltas are versus the average <u><strong>{comparison_label}</strong></u>.",
            unsafe_allow_html=True,
        )

        top_metric_group, top_metric_pax, top_metric_walk_in, top_metric_in_house = st.columns(4)
        top_metric_group.metric(
            "Groups",
            int(selected_day_summary["group_count"]),
            format_metric_delta(
                selected_day_summary["group_count"] - average_group_count,
                comparison_label=comparison_label,
            ),
        )
        top_metric_pax.metric(
            "Pax",
            int(selected_day_summary["guest_count"]),
            format_metric_delta(
                selected_day_summary["guest_count"] - average_guest_count,
                comparison_label=comparison_label,
            ),
        )
        top_metric_walk_in.metric(
            "Walk in (pax)",
            int(selected_day_summary["walk_in_pax"]),
            format_metric_delta(
                selected_day_summary["walk_in_pax"] - average_walk_in_pax,
                comparison_label=comparison_label,
            ),
        )
        top_metric_in_house.metric(
            "In house (pax)",
            int(selected_day_summary["in_house_pax"]),
            format_metric_delta(
                selected_day_summary["in_house_pax"] - average_in_house_pax,
                comparison_label=comparison_label,
            ),
        )

        bottom_metric_longest_wait, bottom_metric_average_wait, bottom_metric_median_wait, bottom_metric_spacer = st.columns(4)
        bottom_metric_longest_wait.metric(
            "Longest wait (min)",
            f"{longest_wait_minutes:.0f} min",
            format_metric_delta(
                longest_wait_minutes - comparison_longest_wait_minutes,
                comparison_label=comparison_label,
                decimals=1,
            ),
        )
        bottom_metric_average_wait.metric(
            "Average wait (min)",
            f"{average_wait_minutes:.1f} min",
            format_metric_delta(
                average_wait_minutes - comparison_average_wait_minutes,
                comparison_label=comparison_label,
                decimals=1,
            ),
        )
        bottom_metric_median_wait.metric(
            "Median wait (min)",
            f"{median_wait_minutes:.1f} min",
            format_metric_delta(
                median_wait_minutes - comparison_median_wait_minutes,
                comparison_label=comparison_label,
                decimals=1,
            ),
        )

        third_row_walkaway, third_row_queue, third_row_queue_hours, third_row_spacer = st.columns(4)
        third_row_walkaway.metric(
            "Walk-aways",
            int(selected_day_summary["walk_aways"]),
            format_metric_delta(
                selected_day_summary["walk_aways"] - comparison_walk_aways,
                comparison_label=comparison_label,
            ),
        )
        third_row_queue.metric(
            "Max queue (groups)",
            int(longest_queue_group_count),
            format_metric_delta(
                longest_queue_group_count - comparison_max_queue_group_count,
                comparison_label=comparison_label,
                decimals=1,
            ),
        )
        third_row_queue_hours.metric(
            "Queue appearance",
            f"{queue_hours:.1f} h",
            format_metric_delta(
                queue_hours - comparison_queue_appearance_hours_value,
                comparison_label=comparison_label,
                decimals=1,
            ),
        )

        gantt_chart = build_daily_seating_gantt(day_df)
        if gantt_chart.data:
            render_plotly_chart(gantt_chart)
        else:
            st.info("No seating duration data is available for this day.")

        load_chart = build_daily_load_chart(day_df, comparison_day_df)
        if load_chart.data:
            render_plotly_chart(load_chart)
        else:
            st.info("No seated or queue data is available for this day.")

        occupancy_chart = build_daily_occupancy_chart(day_df, comparison_day_df)
        if occupancy_chart.data:
            render_plotly_chart(occupancy_chart)
        else:
            st.info("No table occupancy data is available for this day.")


if __name__ == "__main__":
    if streamlit.runtime.exists():
        main()
    else:
        bootstrap.run(str(Path(__file__).resolve()), False, [], {})
