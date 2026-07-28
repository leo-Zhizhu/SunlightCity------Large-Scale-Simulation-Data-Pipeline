"""
Poster-scale version of the monthly exposure chart: one thick line per month, coloured on a
blue -> red ramp ordered by that month's mean exposure, with inline month labels instead of a
legend. The oversized fonts are deliberate — this output targets printed figures and slides.
For the compact on-screen version see plot_annual_exposure.py.

Input: sun_exposure_data.json (produced by plot_annual_exposure.py).
"""

import json
import os
import sys
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import rcParams
import matplotlib.patheffects as patheffects
import matplotlib.colors as mcolors

# Arial for a consistent academic look. matplotlib silently falls back to its default
# sans-serif (and warns) if Arial isn't installed, which is harmless.
rcParams['font.family'] = 'sans-serif'
rcParams['font.sans-serif'] = ['Arial']

def plot_sun_exposure(json_path):
    if not os.path.exists(json_path):
        sys.exit(f"Error: {json_path} not found. Run plot_annual_exposure.py first to generate it.")

    # Load data
    with open(json_path, 'r') as f:
        data = json.load(f)

    months = sorted([int(m) for m in data.keys()])
    if not months:
        sys.exit(f"Error: {json_path} contains no month data.")

    hours = list(range(5, 21))  # 5am to 8pm

    # Calculate average exposure per month for color mapping
    avg_exposure = {}
    for m in months:
        m_data = data[str(m)]
        vals = [v['percentage'] for v in m_data.values()]
        avg_exposure[m] = sum(vals) / len(vals) if vals else 0

    # Rank months by mean exposure, so the colour encodes intensity rather than calendar
    # order — winter months land at the blue end regardless of their month number.
    sorted_by_exposure = sorted(months, key=lambda m: avg_exposure[m])
    month_to_rank = {m: i for i, m in enumerate(sorted_by_exposure)}

    # Guard the rank/(n-1) divisions below against a single-month dataset.
    rank_span = max(1, len(months) - 1)
    
    # Colormap: Deep Blue (lowest exposure) to Exact Red (highest exposure)
    cmap = mcolors.LinearSegmentedColormap.from_list('deep_blue_exact_red', ['#00008B', '#8A2BE2', '#FF0000'])
    
    # Stretching the graph vertically (increased height from 20 to 28)
    plt.figure(figsize=(32, 28), dpi=300)
    
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", 
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Target window for labels: spread around 11:00 to 13:00
    label_x_start = 11.0
    label_x_end = 13.0
    
    for m in months:
        month_data = data[str(m)]
        percentages = []
        valid_hours = []
        
        for h in hours:
            h_str = str(h)
            if h_str in month_data:
                percentages.append(month_data[h_str]['percentage'])
                valid_hours.append(h)
        
        if not percentages:
            continue
            
        rank = month_to_rank[m]
        color = cmap(rank / rank_span)
        
        # Plot lines with high thickness
        plt.plot(valid_hours, percentages, color=color, linewidth=8.5, alpha=0.9)
        
        # Base position
        i = m - 1
        label_x = label_x_start + (i * (label_x_end - label_x_start) / rank_span)

        # Hand-tuned nudges: at 48pt the labels collide around midday where the curves bunch
        # together, so the busiest months get explicit x positions.
        if month_names[i] == "Jun":
            label_x = 11.15
        elif month_names[i] == "Jul":
            label_x = 12.85
        elif month_names[i] == "Aug":
            label_x = 13.45
        elif month_names[i] == "May":
            label_x = 10.55
        elif month_names[i] == "Apr":
            label_x = 11.75
        elif month_names[i] == "Sep":
            label_x = 12.25
        elif month_names[i] == "Mar":
            label_x = 11.45
        elif month_names[i] == "Oct":
            label_x = 12.55
            
        # Linear interpolation for y
        label_y = np.interp(label_x, valid_hours, percentages)
        
        # Ultra-massive 48pt labels with thick stroke
        plt.text(label_x, label_y, month_names[i], 
                 color=color, fontsize=48, fontweight='bold',
                 ha='center', va='center',
                 path_effects=[patheffects.withStroke(linewidth=10, foreground='white')])

    # Ultra-large formatting
    plt.title("Monthly Sun Exposure Variation (5:00 - 20:00)", fontsize=84, fontweight='bold', pad=80)
    plt.xlabel("Time of Day", fontsize=86, fontweight='bold', labelpad=20)
    plt.ylabel("Sun Exposure Percentage (%)", fontsize=86, fontweight='bold', labelpad=20)
    
    plt.yticks(np.arange(0, 101, 10), [f"{i}%" for i in range(0, 101, 10)], fontsize=54, fontweight='bold')
    
    ampm_labels = [f"{h}am" if h < 12 else ("12pm" if h == 12 else f"{h-12}pm") for h in hours]
    plt.xticks(hours, ampm_labels, fontsize=46, fontweight='bold')
    
    ax = plt.gca()
    ax.tick_params(axis='x', which='major', pad=25)
    ax.tick_params(axis='y', which='major', pad=25)
    
    plt.ylim(0, 100)
    plt.xlim(5, 20)
    
    for spine in ax.spines.values():
        spine.set_linewidth(5.0)
    
    plt.grid(True, linestyle='--', alpha=0.4, which='both', linewidth=3.0)
    
    plt.tight_layout()
    
    output_path = "sun_exposure_plot_final.png"
    plt.savefig(output_path, bbox_inches='tight', dpi=300)
    print(f"Vertically stretched 48pt plot saved to {output_path}")

if __name__ == "__main__":
    plot_sun_exposure("sun_exposure_data.json")
