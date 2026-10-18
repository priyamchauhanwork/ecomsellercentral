# Local development setup

How to get **ecomsellercentral** running on a fresh machine, on **macOS** or **Windows**.

| Part | Runs on | Local URL |
|---|---|---|
| Backend API | Python 3.13 + FastAPI, managed with **uv** | http://localhost:8000 (API docs at `/docs`) |
| Frontend | React + Vite, needs **Node.js** (build tooling only) | http://localhost:5173 |
| Database | PostgreSQL 18 in **Docker** | `localhost:5432`, database `mvp_db` |

> On Windows, run every command in **PowerShell**. After installing a tool, open a **new**
> terminal so it is on your PATH.

---

## 1. Install the prerequisites

| Tool | Minimum version | Why |
|---|---|---|
| Git | any recent | Clone the repo |
| uv | 0.8+ | Installs Python and the backend's dependencies |
| Python | 3.13 (installed by uv) | Backend |
| Node.js + npm | 22 LTS (24 LTS recommended) | Frontend tooling only |
| Docker Desktop | any recent | Runs the local PostgreSQL |
| VS Code | any recent | Editor (Python, Pylance, Ruff, ESLint extensions) |
| Postman | optional | API testing (FastAPI's `/docs` page also works) |

### macOS

Install [Homebrew](https://brew.sh) first if you don't have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then:

```bash
# Git (or use the one from Xcode Command Line Tools: xcode-select --install)
brew install git

# uv, then Python 3.13 managed by uv
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.13

# Node.js 24 LTS
brew install node@24
brew link node@24

# Docker Desktop (start it once from Applications after installing)
brew install --cask docker

# psql client (no server; the server runs in Docker)
brew install libpq
echo 'export PATH="$(brew --prefix libpq)/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# Editor and API client
brew install --cask visual-studio-code
brew install --cask postman
```

To use the `code` command in the terminal, open VS Code → Command Palette
(<kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd>) → **Shell Command: Install 'code' command in PATH**.

### Windows

Docker Desktop needs WSL 2. In an **Administrator** PowerShell, run this once and restart:

```powershell
wsl --install
```

Then, in a normal PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
winget install --id OpenJS.NodeJS.LTS -e
winget install --id Docker.DockerDesktop -e
winget install --id Microsoft.VisualStudioCode -e
winget install --id Postman.Postman -e
```

Open a **new** PowerShell, then install Python with uv:

```powershell
uv python install 3.13
```

Start **Docker Desktop** once from the Start menu and wait until it says it is running.

On Windows, `psql` is used through the Docker container (see step 4), so there is nothing
extra to install.

### VS Code extensions (both platforms)

```bash
code --install-extension ms-python.python
code --install-extension ms-python.vscode-pylance
code --install-extension charliermarsh.ruff
code --install-extension dbaeumer.vscode-eslint
```

### Check the versions

```bash
git --version
uv --version
uv run --python 3.13 python --version   # Python 3.13.x
node --version                          # v22 or later
npm --version
docker --version
psql --version                          # macOS only
```

---

## 2. Clone the repository

```bash
git clone https://github.com/priyamchauhanwork/ecomsellercentral.git
cd ecomsellercentral
```

---

## 3. Start the local database

Docker Desktop must be running. This creates a PostgreSQL 18 container with the database
`mvp_db`. Data is kept in the `mvp_pgdata` volume, and the container restarts whenever
Docker starts. The command is the same on macOS and Windows:

```bash
docker run -d --name mvp-postgres --restart unless-stopped -e POSTGRES_USER=mvp -e POSTGRES_PASSWORD=mvp_local -e POSTGRES_DB=mvp_db -p 5432:5432 -v mvp_pgdata:/var/lib/postgresql postgres:18
```

> The volume is mounted at `/var/lib/postgresql`, not `/var/lib/postgresql/data`.
> Postgres 18 images changed their data path; the old path loses data when the
> container is recreated.

These credentials are for local development only. Never use them on Render.

---

## 4. Check the database connection

macOS:

```bash
psql "postgresql://mvp:mvp_local@localhost:5432/mvp_db" -c "select version();"
```

Windows (or macOS without `psql`):

```powershell
docker exec -it mvp-postgres psql -U mvp -d mvp_db -c "select version();"
```

Both should print `PostgreSQL 18.x`.

---

## 5. Create your `.env`

Copy the template. `.env` is git-ignored; real credentials only ever go there.

macOS:

```bash
cp .env.example .env
```

Windows:

```powershell
Copy-Item .env.example .env
```

Then edit `.env`:

| Variable | Local value |
|---|---|
| `PORT` | `8000` |
| `ENVIRONMENT` | `development` |
| `CORS_ORIGINS` | `http://localhost:5173` |
| `API_SHARED_SECRET` | Any long random string, e.g. the output of `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATABASE_URL` | `postgresql://mvp:mvp_local@localhost:5432/mvp_db` |
| `RENDER_DATABASE_URL` | Optional. Render's **external** connection string + `?sslmode=require`, only for running migrations against production |
| `SHOPIFY_STORE_DOMAIN` | Your store domain only, e.g. `your-store.myshopify.com` (no `https://`, no trailing `/`) |
| `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET` | The app's **Client ID** and **Client secret** (`shpss_…`) from the Shopify Dev Dashboard (see #7). The backend turns them into a 24-hour access token automatically. |
| `SHOPIFY_ADMIN_API_ACCESS_TOKEN`, `SHOPIFY_WEBHOOK_SECRET` | Leave empty. The token is only for legacy custom apps created in the Shopify admin; webhooks are verified with the client secret. |
| `SHOPIFY_API_VERSION` | A currently supported version, e.g. `2026-10` |
| `SELLER_*` | Business details printed on invoices and labels |
| `SENDGRID_API_KEY` | From SendGrid; can stay empty locally |
| `SENTRY_DSN` | Leave empty locally to disable Sentry |

---

## 6. Run the backend

Same commands on macOS and Windows:

```bash
cd backend
uv sync                                          # creates .venv and installs dependencies
uv run alembic upgrade head                      # creates the tables in mvp_db
uv run uvicorn app.main:app --reload --port 8000
```

Check it: open http://localhost:8000/health (should show `"db": "ok"`) and
http://localhost:8000/docs for the interactive API docs.

There is no need to activate the virtual environment; `uv run` uses it automatically.

Other backend commands (from `backend/`):

```bash
uv run pytest                                    # run the tests (in a separate mvp_test database, created automatically)
uv run ruff check . && uv run ruff format .      # lint and format
uv run python -m scripts.check_shopify           # test the Shopify connection (needs the Shopify values in .env)
uv run python -m scripts.connect_shopify         # create the store's Shopify connection row (once per database)
```

To run migrations against **Render** from your laptop, override `DATABASE_URL` for one command:

```bash
DATABASE_URL="$RENDER_DATABASE_URL" uv run alembic upgrade head
```

In VS Code, select the backend's interpreter so imports resolve: Command Palette →
**Python: Select Interpreter** → `backend/.venv`.

---

## 7. Run the frontend

> Available once the React app (#15) is merged.

Create `frontend/.env.local` with the API address:

```
VITE_API_URL=http://localhost:8000
```

Then, in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173.

---

## Day-to-day

1. Start Docker Desktop. The `mvp-postgres` container starts with it.
2. Terminal 1: `cd backend` → `uv run uvicorn app.main:app --reload --port 8000`
3. Terminal 2: `cd frontend` → `npm run dev`
4. After pulling changes: `uv sync` and `uv run alembic upgrade head` in `backend/`,
   `npm install` in `frontend/`.

### Useful database commands

```bash
docker ps                                   # is mvp-postgres running?
docker start mvp-postgres                   # start it if stopped
docker exec -it mvp-postgres psql -U mvp -d mvp_db   # open a SQL shell
```

Reset the local database completely (deletes all local data):

```bash
docker rm -f mvp-postgres
docker volume rm mvp_pgdata
```

Then run the `docker run` command from step 3 again.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `port is already allocated` on 5432 | Another PostgreSQL is running locally. Stop it, or use `-p 5433:5432` in `docker run` and port `5433` in `DATABASE_URL`. |
| `Cannot connect to the Docker daemon` | Start Docker Desktop and wait until it is running. |
| `uv`, `node` or `psql` not found right after installing | Open a new terminal (on macOS, or run `source ~/.zshrc`). |
| `warning: VIRTUAL_ENV=/opt/anaconda3 does not match the project environment` | Harmless: the backend always uses its own `.venv` with uv-managed Python (`python-preference = "only-managed"` in `backend/pyproject.toml`). To silence it, stop Anaconda auto-activating its `base` environment: `conda config --set auto_activate_base false`, then open a new terminal. Use `conda activate` when you need Anaconda. |
| Windows: Docker Desktop won't start | Make sure `wsl --install` completed and the machine was restarted; virtualization must be enabled in BIOS. |
| Shopify can't reach `localhost` for webhooks | Expected. Use a tunnel such as `cloudflared` or `ngrok` (see #11). |
