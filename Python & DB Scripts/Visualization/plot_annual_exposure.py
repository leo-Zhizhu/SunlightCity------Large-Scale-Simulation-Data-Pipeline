"""
Plots the diurnal sun-exposure profile for all 12 months on a single axis.

Reads sun_exposure_data.json if it exists; otherwise queries the database month by month and
writes both that JSON cache and a CSV beside it, so the slow aggregate query only ever runs
once. Delete sun_exposure_data.json to force a refresh.
"""

import psycopg2
import matplotlib.pyplot as plt
import calendar
import json
import os
from datetime import datetime

# ── Load Aggregated Exposure Data ─────────────────────────────────────────────
DATA_CACHE_FILE = "sun_exposure_data.json"
conn = None
cursor = None

if os.path.exists(DATA_CACHE_FILE):
    print(f"Loading data from local cache: {DATA_CACHE_FILE}")
    with open(DATA_CACHE_FILE, 'r') as f:
        # Load and ensure keys are integers
        raw_data = json.load(f)
        monthly_data = {int(m): {int(h): d for h, d in h_data.items()} for m, h_data in raw_data.items()}
else:
    # ── DB Connection ──────────────────────────────────────────────────────────
    conn = psycopg2.connect(
        host="localhost",
        port=5432,
        database="city_data",
        user="admin",
        password="password"
    )
    cursor = conn.cursor()

    # We process month by month to prevent timeouts and leverage the index on datetime.
    # We only query for the start of each hour (03:00 - 21:00) as requested.
    print("Querying aggregated exposure data month-by-month...")

    monthly_data = {}
    year = 2026  # Based on date range check

    for month in range(1, 13):
        print(f"  Processing {calendar.month_name[month]}...", end="", flush=True)
        
        # Generate exact timestamps for this month (Start of each hour 3-21)
        num_days = calendar.monthrange(year, month)[1]
        timestamps = []
        for day in range(1, num_days + 1):
            for hour in range(3, 22):
                timestamps.append(datetime(year, month, day, hour, 0, 0))
        
        if not timestamps:
            print(" Skipped (No days)")
            continue

        # Enumerating exact timestamps (rather than a BETWEEN range plus a date cast) lets
        # Postgres use idx_meo_exposure_samples_time directly, which matters against a
        # 1.57-billion-row table. Both the sunlit and total counts are fetched so the
        # percentage is normalised against however many points actually exist.
        query = """
            SELECT 
                EXTRACT(HOUR FROM datetime) as hour, 
                SUM(is_sunlit::int) as sunlit_count,
                COUNT(*) as total_count
            FROM meo_exposure_samples
            WHERE datetime IN %s
            GROUP BY hour;
        """
        
        cursor.execute(query, (tuple(timestamps),))
        rows = cursor.fetchall()
        
        monthly_data[month] = {}
        for h, sunlit, total in rows:
            # Calculate percentage of sunlit points
            percentage = (sunlit / total) * 100 if total > 0 else 0
            monthly_data[month][int(h)] = {
                'sunlit_count': int(sunlit),
                'total_count': int(total),
                'percentage': float(percentage)
            }
        
        print(" Success.")
    
    # Save to local cache (JSON)
    with open(DATA_CACHE_FILE, 'w') as f:
        json.dump(monthly_data, f, indent=4)
    
    # Save to CSV for easy inspection
    import csv
    csv_filename = "sun_exposure_data.csv"
    with open(csv_filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Month", "Hour", "Sunlit_Count", "Total_Count", "Percentage"])
        for m in sorted(monthly_data.keys()):
            for h in sorted(monthly_data[m].keys()):
                d = monthly_data[m][h]
                writer.writerow([m, h, d['sunlit_count'], d['total_count'], f"{d['percentage']:.2f}"])
    
    print(f"Data saved to {DATA_CACHE_FILE} and {csv_filename} for future use.")

months = sorted(monthly_data.keys())
month_names = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"
}

# ── Plotting (Academic Style) ────────────────────────────────────────────────
plt.rcParams.update({
    "font.family": "serif",
    "font.size": 11,
    "axes.labelsize": 12,
    "axes.titlesize": 14,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "figure.titlesize": 16
})

fig, ax = plt.subplots(figsize=(18, 10), dpi=300)
fig.patch.set_facecolor('white')
ax.set_facecolor('#fcfcfc')

# High-contrast colormap for 12 months.
# plt.get_cmap replaces matplotlib.cm.get_cmap, which was deprecated in 3.7 and removed in 3.9.
color_map = plt.get_cmap('turbo', 12)

for i, month in enumerate(months):
    hours_data = monthly_data[month]
    sorted_hours = sorted(hours_data.keys())
    # Extract percentage for plotting
    values = [hours_data[h]['percentage'] if isinstance(hours_data[h], dict) else hours_data[h] for h in sorted_hours]
    
    # Map month 1-12 to color index 0-11
    color = color_map((month - 1) / 11.0)
    
    ax.plot(sorted_hours, values, 
            label=month_names[month], 
            color=color, 
            linewidth=2.5, 
            marker='o', 
            markersize=4, 
            markerfacecolor='white', 
            markeredgewidth=1.5,
            zorder=13 - month)

# ── Formatting ───────────────────────────────────────────────────────────────
ax.set_xlabel("Hour of Day (24h format)", labelpad=12)
ax.set_ylabel("Sunlit Cells Percentage (%)", labelpad=12)
ax.set_title("Annual Variation of Sunlit Cell Percentage (Diurnal Profile)", pad=25, fontweight='bold', fontsize=18)
ax.set_ylim(-2, 102) # Ensure 0-100% is visible with some padding

# Set x-ticks to be every hour
all_present_hours = sorted(set(h for m_data in monthly_data.values() for h in m_data.keys()))
if all_present_hours:
    ax.set_xticks(range(min(all_present_hours), max(all_present_hours) + 1))
    ax.set_xticklabels([f"{h:02d}:00" for h in range(min(all_present_hours), max(all_present_hours) + 1)], rotation=0)

ax.grid(True, linestyle='--', alpha=0.6, color='#dddddd', zorder=0)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_linewidth(1.2)
ax.spines['bottom'].set_linewidth(1.2)

# Legend outside the plot area
ax.legend(title="Months", loc='center left', bbox_to_anchor=(1, 0.5), frameon=True, shadow=False)

plt.tight_layout()

# ── Save and Show ────────────────────────────────────────────────────────────
output_filename = "total_sun_exposure_annual.png"
plt.savefig(output_filename, bbox_inches='tight')
print(f"Success! Plot saved to {output_filename}")

if cursor:
    cursor.close()
if conn:
    conn.close()
plt.show()
