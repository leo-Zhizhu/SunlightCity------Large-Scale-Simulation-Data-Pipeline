# SunlightCity Simulation Setup Guide

Welcome to the SunlightCity project! This guide will walk you through setting up the necessary database and opening the Unity project so you can run the simulation locally.

> **This guide covers the v1, single-node pipeline** — one desktop, one PostgreSQL
> instance, a 6-hour annual run. That is the reference implementation and the right place
> to start.
>
> | If you want to… | Read |
> |---|---|
> | understand what v1 does, phase by phase | [`docs/V1_PIPELINE.md`](docs/V1_PIPELINE.md) |
> | run it across a Kubernetes fleet in ~3 minutes | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
> | know why the distributed version needs 11 databases | [`docs/DB_CLUSTER.md`](docs/DB_CLUSTER.md) |
> | see the whole picture first | [`README.md`](README.md) |

## Prerequisites
Before you begin, ensure you have the following installed:
1. **Docker Desktop** (or Docker Engine with Docker Compose)
2. **Unity Hub** and the required **Unity Editor version** for this project

## Step 1: Database Setup
This project relies on a PostGIS database to store and retrieve simulation data. We use Docker to easily run the database locally without complex installations.

> [!IMPORTANT]
> **Data Import Requirement:** You should have received a separate data file named `99_data_dump.sql.gz`. 
> You MUST place this file inside the `db/` folder **before** starting the database for the first time.

1. Ensure the `99_data_dump.sql.gz` file is placed inside the `db/` folder.
2. Open your terminal (or command prompt / PowerShell).
3. Navigate to the root directory of this project (where `docker-compose.yml` is located).
4. Run the following command to start the database container:
   ```bash
   docker-compose up -d
   ```
   *Note: On first run, Docker will execute the scripts in the `db/` folder. It will set up the schema and automatically import the data from your `.gz` file. This may take a moment depending on the data size.*

4. To verify the database is running, you can use:
   ```bash
   docker ps
   ```
   You should see a container named `postgis_city` running on port `5432`.

## Step 2: Opening the Unity Project
1. Open **Unity Hub**.
2. Click the **Add** dropdown (or simply **Open** in newer Unity Hub versions) and select **Add project from disk**.
3. Browse to the directory where you extracted this project and select the root folder (the one containing `Assets`, `ProjectSettings`, etc.).
4. Unity Hub will add the project to your list. It will also show you which Unity Editor version the project uses. If you don't have that version installed, Unity Hub will prompt you to install it.
5. Click on the project in Unity Hub to open it.

*Note: The first time you open the project, Unity will recreate the `Library` folder and import all the assets. This may take a few minutes.*

## Step 3: Running the Project
Once the project finishes importing and opens in the Unity Editor:
1. Ensure the `postgis_city` Docker container is still running.
2. Open the main simulation scene from the `Assets/` folder.
3. Press the **Play** button at the top of the Unity Editor to start the simulation.

## Troubleshooting
- **Database Connection Issues:** Ensure Docker is running and no other local services are using port `5432` on your machine.
- **Missing Packages/Resources:** Do not worry about missing resources on the first open; Unity's Package Manager will automatically fetch the required packages defined in the project settings.
- **Empty Database:** If you ran `docker-compose up -d` before moving the `99_data_dump.sql.gz` file into the `db/` folder, the database was initialized empty. To fix this, run `docker-compose down -v` to delete the empty volume, ensure the `.gz` file is in the `db/` folder, and then run `docker-compose up -d` again.
