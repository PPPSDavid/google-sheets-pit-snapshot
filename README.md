# Google Sheets point-in-time snapshot tool

A Python script that creates **point-in-time (PIT) copies** of selected Google Sheets in Google Drive. Copies are full spreadsheets (not CSV exports) and can be placed next to the originals or in a dedicated folder.

- **Cross-references** (e.g. `IMPORTRANGE`) are detected and **rewritten** so they point to the PIT copies instead of the live originals, keeping the snapshot self-consistent and frozen in time.
- If any formula references a spreadsheet **not** in your snapshot list, the tool **warns** you (value innovation risk) and recommends adding those sheets to the snapshot.

## Setup

### 1. Google Cloud project and APIs

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project (or use an existing one).
2. Enable **Google Sheets API** and **Google Drive API**: APIs & Services → Enable APIs → enable both.
3. Choose **how you want to authenticate** (see below).

### 2. Credentials: personal (OAuth) or service account

The script supports **two credential types**. You can start with **personal/OAuth** for testing, then switch to a **service account** for automation.

#### Option A — Personal account (OAuth, good for testing)

Use your own Google account. The first run opens a browser to sign in and grant access; later runs reuse a saved token.

1. In Cloud Console: **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. If prompted, configure the **OAuth consent screen** (e.g. External). **Important:** while the app is in **Testing** mode, only listed test users can sign in. Go to **APIs & Services → OAuth consent screen** → **Test users** → **+ ADD USERS** and add the Google account you use to run the script (e.g. `your@gmail.com`). Otherwise you’ll get “Access blocked: … has not completed the Google verification process” (Error 403).
3. Choose **Desktop app** as application type. Create the client and **download the JSON**.
4. Save the file as `credentials_oauth.json` in this project (or any path; pass it with `-c`).
5. **No need to share any sheet** with an email—you’re using your own account, so you already have access to your sheets.

Run the script with that file:

```bash
pip install -r requirements.txt
python snapshot_sheets.py -c credentials_oauth.json -n "YOUR_SPREADSHEET_ID"
```

The first time, a browser window opens so you can sign in and allow access. The script saves a `token.json` next to your credentials file so you won’t be prompted again until the token expires or is revoked. If Google shows “This app isn’t verified”, use **Advanced** → **Go to … (unsafe)** to continue—this is your own project.

#### Option B — Service account (for automation / production)

No browser; the script uses a robot account. You must share each sheet with that account.

1. **APIs & Services → Credentials → Create credentials → Service account**. Create it and open it.
2. **Keys → Add key → Create new key → JSON**. Save as `credentials.json` (or another path; use `-c`).
3. In the JSON file, find **`client_email`** (e.g. `...@project-id.iam.gserviceaccount.com`).
4. For each Google Sheet you want to snapshot: open the sheet → **Share** → add that email as **Viewer**.

Run with the service account key (default is `credentials.json`):

```bash
python snapshot_sheets.py -n "YOUR_SPREADSHEET_ID"
```

**How the script chooses credentials**

- The script looks at the **contents** of the file you pass with `-c` (default: `credentials.json`).
- If the file contains **`"installed"` or `"web"`** (and no `"private_key"`), it is treated as **OAuth client secrets** → personal sign-in and `token.json`.
- If it contains **`"private_key"`**, it is treated as a **service account** → no browser, no sharing of token files.

So you can use **one file for OAuth** (e.g. `credentials_oauth.json`) and **another for service account** (e.g. `credentials.json`), and switch by changing `-c`.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

## Development / testing

You can **test with your personal account first**, then move to a service account:

1. Create a GCP project and enable **Sheets** and **Drive** APIs.
2. Create **OAuth client ID** (Desktop app), download JSON, save as e.g. `credentials_oauth.json`.
3. Run:  
   `python snapshot_sheets.py -c credentials_oauth.json -n "YOUR_SPREADSHEET_ID"`  
   Sign in in the browser when prompted. The script will create a PIT copy in your Drive.
4. When you’re ready to automate, create a **service account**, download its JSON, share your sheets with its `client_email`, and run with `-c credentials.json` (or omit `-c` if the file is `credentials.json`).

## Usage

**Snapshot one or more spreadsheets (by ID or URL):**

```bash
python snapshot_sheets.py "1ABC...xyz" "https://docs.google.com/spreadsheets/d/1DEF.../edit"
```

**Using environment variable (space-separated IDs or URLs):**

```bash
set SHEET_IDS=1ABC...xyz https://docs.google.com/spreadsheets/d/1DEF.../edit
python snapshot_sheets.py
```

**Where copies go**

- **Destination folder with PIT subfolder**: pass a Drive folder ID with `-d`; the script creates a timestamped subfolder inside it (e.g. `PIT Snapshots 2025-02-15 14-30-22`) and puts all copies there. Env: `PIT_DESTINATION_FOLDER_ID`.
- **Single Drive folder** (`-o`): all copies go directly in that folder. Env: `PIT_OUTPUT_FOLDER_ID`.
- **New folder in Drive root**: omit `-d` and `-o`; the script creates e.g. `PIT Snapshots 2025-02-15 14-30-22` in root.

**Options**

- `-c, --credentials` — Path to credentials JSON. Default: `credentials.json` or `GOOGLE_APPLICATION_CREDENTIALS`.
- `-d, --destination-folder` — Drive folder ID; a PIT subfolder is created inside it.
- `-o, --output-folder-id` — Drive folder ID; copies go directly in this folder.
- `-t, --timestamp` — Timestamp for copy names (default: current time).
- `--no-warn-external` — Do not warn about formulas referencing spreadsheets not in the snapshot list.
- `--test` — Only test connectivity (no snapshot).
- `-v, --verbose` — Debug logging.

---

### Quick test with multiple sheets (e.g. 3 sheets into a destination folder)

1. **Get each spreadsheet ID**  
   In Drive, open each sheet (e.g. your three “model locker test” sheets). In the browser URL you’ll see:  
   `https://docs.google.com/spreadsheets/d/`**`SPREADSHEET_ID`**`/edit`  
   Copy the `SPREADSHEET_ID` for each of the 3 sheets (e.g. `1abc...`, `1def...`, `1ghi...`).

2. **Get the destination folder ID (folder A)**  
   Open the Drive folder where you want PIT snapshots to live (e.g. “Misc” or “Personal”). In the URL:  
   `https://drive.google.com/drive/folders/`**`FOLDER_ID`**  
   Copy that `FOLDER_ID`.

3. **Run the snapshot** (OAuth example; use your credentials path and IDs):
   ```bash
   python snapshot_sheets.py -c credentials_oauth.json -d FOLDER_A_ID "id1" "id2" "id3"
   ```
   Replace `FOLDER_A_ID` and `id1`, `id2`, `id3` with the IDs from steps 1–2. The script will create a subfolder like `PIT Snapshots 2025-02-15 14-30-22` inside folder A and put the three PIT copies there.

### Validating a PIT run

1. **Location** — In Google Drive, open **My Drive** and find the destination (e.g. folder **Test Sheet PIT Snapshot**). Inside it you should see a timestamped subfolder (e.g. **PIT Snapshots 2026-02-15 16-55-50**) and inside that, one copy per source sheet (e.g. *model locker test sheet 1 (PIT …)*, etc.).
2. **Count** — Confirm the number of copied spreadsheets matches the number of IDs you passed.
3. **Data** — Open one or more PIT copies and spot-check cells: values should match the originals at the time of the run.
4. **Cross-references** — If your originals use `IMPORTRANGE` (or similar) pointing at each other, open a PIT copy and confirm the formula bar shows the updated reference to the PIT copy IDs. The first time you open a PIT copy, Google may show “You need to connect these spreadsheets”; click **Allow access** so IMPORTRANGE can load (see [First time you open a PIT copy](#first-time-you-open-a-pit-copy-importrange-allow-access) below).

## Cross-references and warnings

- The script looks for **IMPORTRANGE** (and spreadsheet URLs in formulas) and rewrites them so the **PIT copies** reference each other instead of the live originals.
- **Same-spreadsheet tab references** (e.g. `='PnL (linked)'!B7`) can break after a Drive copy because internal sheet IDs change. The script re-writes these formulas unchanged so Sheets re-resolves them; the PIT copy should then show the correct values.
- If a formula references a spreadsheet that is **not** in the list you pass to the script, you’ll see a **warning**: that reference will still point at the live file, so when that file changes, your PIT copy will show new values (value innovation). The script recommends adding those spreadsheet IDs to your snapshot list and re-running.

### First time you open a PIT copy (IMPORTRANGE “Allow access”)

When a PIT copy contains **IMPORTRANGE** formulas that pull from other PIT copies (or any other spreadsheet), Google Sheets requires a **one-time manual permission** the first time that link is used.

- **What you’ll see:** A dialog like “You need to connect these spreadsheets. The first time the destination spreadsheet pulls data from a new source spreadsheet, permission is needed to be granted,” with an **Allow access** button. The affected cells may show `#REF!` until you grant access.
- **What to do:** Click **Allow access** in the dialog. After that, the IMPORTRANGE formulas will work and the data will load. You only need to do this once per (destination sheet, source sheet) pair.
- **Why it’s not avoidable:** Google does not provide an API to grant this connection programmatically; it’s a deliberate security step so that one spreadsheet cannot pull data from another without user consent. The script cannot remove this step while keeping live IMPORTRANGE formulas.

## Example

```bash
python snapshot_sheets.py -d YOUR_FOLDER_ID "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
```

This creates a PIT subfolder inside `YOUR_FOLDER_ID`, copies the spreadsheet there, and updates any `IMPORTRANGE` (or similar) references to point to the new copy. Output is logged to stderr (use `-v` for debug).
