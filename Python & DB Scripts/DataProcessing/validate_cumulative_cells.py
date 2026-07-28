"""
Sanity check: totals exposed vs. total sample cells per month and bar-charts the result.

Use it to confirm an export produced a plausible seasonal curve — exposed cells should peak in
summer and dip in winter. A month that comes back flat or missing means the export never
covered it.
"""

import psycopg2
import matplotlib.pyplot as plt

# -- DB Connection --------------------------------------------------------------
conn = psycopg2.connect(
    host="localhost",
    port=5432,
    database="city_data",
    user="admin",
    password="password"
)
cursor = conn.cursor()

print("Executing database aggregation for 12 months (this may take a few minutes)...")

# Aggregate by month entirely server-side. The detail table holds ~1.57 billion rows, so
# fetching it into Python to group in-process would exhaust memory; this returns only 12 rows.
# The sample_point_id foreign key already enforces referential integrity, so the previous
# `WHERE sample_point_id IN (SELECT id FROM meo_sample_points)` filter was a no-op semi-join
# over the whole table and has been dropped.
sql_query = """
            SELECT to_char(e.datetime, 'YYYY-MM') AS month_key,
                   SUM(e.is_sunlit::int)          AS exposed_cells,
                   COUNT(e.sample_point_id)       AS total_cells
            FROM meo_exposure_samples e
            GROUP BY month_key
            ORDER BY month_key;
            """

cursor.execute(sql_query)
results = cursor.fetchall()

sorted_months = []
exposed_values = []
total_values = []

print("\nMonthly summary:")
print(f"{'Month':>8} | {'Exposed cells':>14} | {'Total cells':>12} | {'Exposed ratio':>13}")
print("-" * 60)

for row in results:
    month, exposed, total = row
    sorted_months.append(month)
    exposed_values.append(exposed)
    total_values.append(total)

    ratio = exposed / total if total else 0
    print(f"{month:>8} | {exposed:>14,} | {total:>12,} | {ratio:>13.4f}")

# -- Plot (bar chart + exposed value labels) -----------------------------------
# Extra-wide canvas so all 12 month labels stay legible.
fig, ax = plt.subplots(figsize=(14, 6))
fig.patch.set_facecolor("white")
ax.set_facecolor("white")

x = list(range(len(sorted_months)))
bar_width = 0.42

total_bars = ax.bar(
    [i - bar_width / 2 for i in x],
    total_values,
    width=bar_width,
    color="#1a7abf",
    alpha=0.85,
    label="Total cells",
    zorder=3
)
exposed_bars = ax.bar(
    [i + bar_width / 2 for i in x],
    exposed_values,
    width=bar_width,
    color="#e05c2a",
    alpha=0.9,
    # The export window is 03:00-21:00, not a full 24h day.
    label="Exposed cells (03:00-21:00 sum)",
    zorder=3
)

# Label each exposed bar with its value.
for bar, value in zip(exposed_bars, exposed_values):
    ax.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + max(total_values) * 0.01,
        f"{value:,}",
        ha="center",
        va="bottom",
        fontsize=8,
        color="#7a2f13"
    )

ax.set_xlabel("Month", color="#333333", fontsize=10, labelpad=8)
ax.set_ylabel("Number of cells", color="#333333", fontsize=10, labelpad=8)
ax.set_title("Monthly total cells and exposed cells", color="#111111", fontsize=13, pad=14)
ax.set_xticks(x)
ax.set_xticklabels(sorted_months, rotation=45, ha="right")

ax.grid(axis="y", color="#e0e0e0", linewidth=0.8, zorder=0)
ax.grid(axis="x", color="#eeeeee", linewidth=0.5, zorder=0)

for spine in ax.spines.values():
    spine.set_edgecolor("#cccccc")
ax.tick_params(colors="#555555")

ax.legend(
    loc="upper right",
    fontsize=9,
    framealpha=0.8,
    facecolor="white",
    edgecolor="#cccccc",
    labelcolor="#111111"
)

plt.tight_layout()
filename = "monthly_exposure_totals_12_months.png"
plt.savefig(filename, dpi=150, facecolor=fig.get_facecolor())
plt.show()
print(f"\nSaved: {filename}")

cursor.close()
conn.close()