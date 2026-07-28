"""Windows helper: launches db_robust_export.ps1 in a detached PowerShell window so a
multi-hour pg_dump of the ~110 GB database survives the IDE being closed."""

import subprocess
import os

def launch_robust_export():
    # Must match the filename on disk. This previously referenced "robust_export.ps1" and so
    # always aborted with "Script not found" after the script was renamed.
    script_path = os.path.join(os.path.dirname(__file__), "db_robust_export.ps1")

    if not os.path.exists(script_path):
        print(f"Error: Script not found at {script_path}")
        return

    print("Launching robust export in a new PowerShell window...")
    print("This will allow the export to continue even if this IDE shuts down.")
    
    # Command to start a new PowerShell window and run the script
    # -NoExit keeps the window open so the user can see the final status
    cmd = [
        "powershell.exe",
        "Start-Process", "powershell.exe",
        "-ArgumentList", f"'-NoExit', '-File', '{script_path}'"
    ]
    
    try:
        subprocess.run(cmd, check=True)
        print("\nProcess launched! Please check the new PowerShell window for progress.")
        print("A log file will also be created in the current directory.")
    except Exception as e:
        print(f"Failed to launch robust export: {e}")
        print(f"You can manually run it with: powershell -File {script_path}")

if __name__ == "__main__":
    launch_robust_export()

