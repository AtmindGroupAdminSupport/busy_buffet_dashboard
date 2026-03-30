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
    sizeref = max(max_count / (26**2), 1 / (26**2))
    sizes = positive_counts.clip(lower=1).tolist()
    return {
        "size": sizes,
        "sizemode": "area",
        "sizeref": sizeref,
        "sizemin": 8,
        "color": color,
        "symbol": symbol,
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
    if len(name) >= 3 and name[:3].isdigit():
        day = int(name[:2])
        month = int(name[2])
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
        df["date"] = pd.Timestamp.now().normalize()

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


@st.cache_data(show_spinner=False)
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
        sheet_df["date"] = parsed_date if parsed_date is not None else pd.Timestamp.now().normalize()
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

    if sheet_dates:
        normalized_sheet_dates = pd.to_datetime(pd.Series(sheet_dates)).dt.normalize().dropna().drop_duplicates().sort_values()
        full_dates = pd.DataFrame({"date": pd.date_range(normalized_sheet_dates.min(), normalized_sheet_dates.max(), freq="D")})
        daily = full_dates.merge(grouped, on="date", how="left")
        daily["has_sheet_data"] = daily["date"].isin(set(normalized_sheet_dates.tolist()))
    else:
        daily = grouped.copy()
        daily["has_sheet_data"] = True

    for column in ["waited_groups", "avg_wait_minutes", "walk_aways", "guest_count"]:
        daily[column] = daily[column].fillna(0)

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
    daily["date_label"] = daily["date"].dt.strftime("%Y-%m-%d")
    daily["walkaway_delta"] = daily["walk_aways"].diff().fillna(daily["walk_aways"]).astype(int)
    return daily


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
    guest_point_mask = daily["guest_count"].notna() & daily["has_sheet_data"].fillna(False)
    walkaway_labels = daily["walk_aways"].apply(lambda value: f"WA: {int(value)}" if value > 0 else "")
    guest_count_labels = daily["guest_count"].apply(lambda value: str(int(value)) if pd.notna(value) else "")
    queue_line = mask_series_for_missing_days(daily["waited_groups"], daily["has_sheet_data"])
    guest_count_line = mask_series_for_missing_days(daily["guest_count"], daily["has_sheet_data"])
    queue_gap_pairs = find_missing_day_gap_pairs(daily["waited_groups"], daily["has_sheet_data"])
    guest_gap_pairs = find_missing_day_gap_pairs(daily["guest_count"], daily["has_sheet_data"])
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
        x=daily["date_label"],
        y=guest_count_line,
        name="Guest count",
        mode="lines",
        yaxis="y2",
        line=dict(color=hex_to_rgba(PALETTE["coral"], 0.55), width=2),
        hovertemplate="Date: %{x}<br>Guest count: %{y}<extra></extra>",
        legendrank=3,
    )
    fig.add_scatter(
        x=daily.loc[guest_point_mask, "date_label"],
        y=daily.loc[guest_point_mask, "guest_count"],
        name="Guest count labels",
        mode="markers+text",
        text=guest_count_labels[guest_point_mask],
        textposition="top center",
        yaxis="y2",
        marker=dict(size=8, color=hex_to_rgba(PALETTE["coral"], 0.55), symbol="circle"),
        hovertemplate="Date: %{x}<br>Guest count: %{y}<extra></extra>",
        showlegend=False,
    )
    if guest_gap_pairs:
        fig.add_scatter(
            x=[item for start, end in guest_gap_pairs for item in (daily.iloc[start]["date_label"], daily.iloc[end]["date_label"], None)],
            y=[item for start, end in guest_gap_pairs for item in (daily.iloc[start]["guest_count"], daily.iloc[end]["guest_count"], None)],
            name="Guest count gap",
            mode="lines",
            yaxis="y2",
            line=dict(color=hex_to_rgba(PALETTE["coral"], 0.55), width=2, dash="dot"),
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
        title="Queues plus walk-aways area by date",
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


def build_daily_seating_gantt(day_df: pd.DataFrame) -> go.Figure:
    seating_rows: list[dict[str, object]] = []
    for row in day_df.itertuples(index=False):
        if pd.isna(row.meal_start) or pd.isna(row.meal_end):
            continue
        for table in expand_table_no(row.table_no):
            if table == QUEUING_AREA_TABLE:
                continue
            seating_rows.append(
                {
                    "table_no": table,
                    "meal_start": row.meal_start,
                    "meal_end": row.meal_end,
                    "guest_type": row.guest_type if getattr(row, "guest_type", "") else "Unknown",
                    "service_no": getattr(row, "service_no", ""),
                    "duration_minutes": max(0.0, (row.meal_end - row.meal_start).total_seconds() / 60),
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


def build_daily_load_chart(day_df: pd.DataFrame) -> go.Figure:
    occupancy_slices: list[pd.DataFrame] = []
    for row in day_df.itertuples(index=False):
        if pd.isna(row.meal_start) or pd.isna(row.meal_end):
            continue
        time_index = pd.date_range(
            start=row.meal_start.floor("min"),
            end=row.meal_end.ceil("min"),
            freq="1min",
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
        time_index = pd.date_range(
            start=row.queue_start.floor("min"),
            end=row.queue_end.ceil("min"),
            freq="1min",
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
        return go.Figure()

    start_time = min(frame["time"].min() for frame in [seated, queued] if not frame.empty)
    end_time = max(frame["time"].max() for frame in [seated, queued] if not frame.empty)
    load = pd.DataFrame({"time": pd.date_range(start_time, end_time, freq="1min")})
    load = load.merge(seated, on="time", how="left").merge(queued, on="time", how="left").fillna(0)

    fig = go.Figure()
    fig.add_bar(x=load["time"], y=load["seated"], name="Seated tables", marker_color=PALETTE["orange"])
    fig.add_bar(x=load["time"], y=load["queue"], name="Waiting groups", marker_color=PALETTE["berry"])
    fig.update_layout(
        title="Stacked seated and waiting load by time of day",
        xaxis_title="Time",
        yaxis_title="Count",
        barmode="stack",
        template="plotly_white",
        hovermode="x unified",
    )
    return fig


def build_daily_occupancy_chart(day_df: pd.DataFrame) -> go.Figure:
    occupancy_slices: list[pd.DataFrame] = []
    for row in day_df.itertuples(index=False):
        if pd.isna(row.meal_start) or pd.isna(row.meal_end):
            continue
        time_index = pd.date_range(
            start=row.meal_start.floor("min"),
            end=row.meal_end.ceil("min"),
            freq="1min",
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
        return go.Figure()

    occupancy_df = pd.concat(occupancy_slices)
    occupancy_by_guest_type = (
        occupancy_df.groupby(["time", "guest_type"])["table_no"]
        .nunique()
        .reset_index(name="tables_occupied")
    )
    fig = px.bar(
        occupancy_by_guest_type,
        x="time",
        y="tables_occupied",
        color="guest_type",
        barmode="stack",
        color_discrete_map={"In house": PALETTE["coral"], "Walk in": PALETTE["amber"], "Unknown": PALETTE["sand"]},
        labels={
            "time": "Time",
            "tables_occupied": "Tables occupied",
            "guest_type": "Guest type",
        },
        title="Table occupancy by time of day",
    )
    fig.update_layout(template="plotly_white", hovermode="x unified")
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
        render_plotly_chart(build_queue_with_walkaway_area_chart(daily))
        st.markdown("*WA = walk away | Time on bar is peak of queue waiting*")

        peak_wait_row = daily.loc[daily["avg_wait_minutes"].idxmax()]
        peak_walkaway_row = daily.loc[daily["walk_aways"].idxmax()]
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
                daily[["date_label", "guest_count", "waited_groups", "avg_wait_minutes", "walk_aways"]],
                use_container_width=True,
            )

    with daily_tab:
        available_dates = sorted(df["date"].dropna().dt.normalize().unique())
        if not available_dates:
            st.info("No daily records are available for detailed analytics.")
            return

        selected_date = st.selectbox(
            "Choose a day",
            options=available_dates,
            format_func=lambda value: pd.Timestamp(value).strftime("%Y-%m-%d"),
        )
        day_df = df[df["date"] == pd.Timestamp(selected_date).normalize()].copy()

        gantt_chart = build_daily_seating_gantt(day_df)
        if gantt_chart.data:
            render_plotly_chart(gantt_chart)
        else:
            st.info("No seating duration data is available for this day.")

        load_chart = build_daily_load_chart(day_df)
        if load_chart.data:
            render_plotly_chart(load_chart)
        else:
            st.info("No seated or queue data is available for this day.")

        occupancy_chart = build_daily_occupancy_chart(day_df)
        if occupancy_chart.data:
            render_plotly_chart(occupancy_chart)
        else:
            st.info("No table occupancy data is available for this day.")


if __name__ == "__main__":
    if streamlit.runtime.exists():
        main()
    else:
        bootstrap.run(str(Path(__file__).resolve()), False, [], {})
