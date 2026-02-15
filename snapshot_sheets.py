#!/usr/bin/env python3
"""
Point-in-time (PIT) snapshot tool for Google Sheets.
Creates copies of spreadsheets in Google Drive and rewrites cross-references
(IMPORTRANGE, etc.) to point to the PIT copies so the snapshot stays self-consistent.
Warns when formulas reference sheets not included in the snapshot.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import click
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as OAuth2Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Drive: copy + create folder; Sheets: read + write (for formula updates)
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# IMPORTRANGE("url_or_id", "range") - capture prefix, quote, and first argument
IMPORTRANGE_PATTERN = re.compile(
    r"(IMPORTRANGE\s*\(\s*)([\"'])([^\"']+)\2",
    re.IGNORECASE,
)
# URL form: /d/ID/ or /d/ID
URL_ID = re.compile(r"/d/([\w\-]+)(?:/|$)")

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool = False) -> None:
    """Configure logging level and format."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def extract_sheet_id(url_or_id: str) -> str:
    """Extract spreadsheet ID from a URL or return as-is if already an ID."""
    s = url_or_id.strip()
    if re.match(r"^[\w\-]{40,}$", s):
        return s
    m = re.search(r"/d/([\w\-]+)", url_or_id)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot parse sheet ID from: {url_or_id}")


def spreadsheet_ids_in_formula(formula: str) -> set[str]:
    """Extract all spreadsheet IDs referenced in a formula (IMPORTRANGE, URLs)."""
    ids = set()
    if not formula or not isinstance(formula, str):
        return ids
    # IMPORTRANGE first argument (URL or ID) is group(3)
    for match in IMPORTRANGE_PATTERN.finditer(formula):
        arg = match.group(3).strip()
        if URL_ID.search(arg):
            ids.add(URL_ID.search(arg).group(1))
        elif re.match(r"^[\w\-]{40,}$", arg):
            ids.add(arg)
    # Any other docs.google.com/spreadsheets/d/ID in formula (e.g. HYPERLINK, manual links)
    for match in URL_ID.finditer(formula):
        ids.add(match.group(1))
    return ids


def replace_spreadsheet_refs_in_formula(formula: str, id_mapping: dict[str, str]) -> str:
    """Replace referenced spreadsheet IDs in formula with PIT copy IDs. Returns new formula."""
    if not formula or not id_mapping:
        return formula
    result = formula
    # Replace by longest first so we don't replace substrings of IDs
    for old_id, new_id in sorted(id_mapping.items(), key=lambda x: -len(x[0])):
        # Replace URL form: /d/OLD_ID/ or /d/OLD_ID at end
        result = re.sub(
            re.escape(f"/d/{old_id}") + r"(?=/|$)",
            f"/d/{new_id}",
            result,
        )
        # IMPORTRANGE("...first_arg...", "range") - rewrite first argument
        def repl_slash(m):
            """Preserve trailing / after ID so URL stays .../d/NEW_ID/edit not .../d/NEW_IDedit"""
            return f"/d/{new_id}" + ("/" if m.group(0).endswith("/") else "")

        def repl_slash_other(m):
            other_new = id_mapping.get(m.group(1), m.group(1))
            return f"/d/{other_new}" + ("/" if m.group(0).endswith("/") else "")

        def repl_import(match):
            pre, quote, arg = match.group(1), match.group(2), match.group(3)
            if arg.strip() == old_id:
                return f"{pre}{quote}{new_id}{quote}"
            url_match = URL_ID.search(arg)
            if url_match and url_match.group(1) == old_id:
                new_arg = URL_ID.sub(repl_slash, arg, count=1)
                return f"{pre}{quote}{new_arg}{quote}"
            if url_match and url_match.group(1) in id_mapping:
                new_arg = URL_ID.sub(repl_slash_other, arg, count=1)
                return f"{pre}{quote}{new_arg}{quote}"
            return match.group(0)

        result = IMPORTRANGE_PATTERN.sub(
            lambda m: repl_import(m),
            result,
        )
        # Standalone quoted ID (e.g. IMPORTRANGE("id", "A1"))
        result = result.replace(f'"{old_id}"', f'"{new_id}"')
        result = result.replace(f"'{old_id}'", f"'{new_id}'")
    return result


def col_letter(col_index: int) -> str:
    """0 -> A, 1 -> B, ..., 26 -> AA, etc."""
    result = []
    n = col_index
    while True:
        result.append(chr(ord("A") + (n % 26)))
        n = n // 26
        if n == 0:
            break
        n -= 1
    return "".join(reversed(result))


def a1_notation(sheet_title: str, row_index: int, col_index: int) -> str:
    """Sheet title (quoted if needed) + A1 notation for row/col (0-based)."""
    safe = sheet_title if re.match(r"^[A-Za-z0-9_]+$", sheet_title) else f"'{sheet_title}'"
    return f"{safe}!{col_letter(col_index)}{row_index + 1}"


def _is_oauth_client_secrets(path: str) -> bool:
    """Return True if the JSON file is OAuth client secrets (Desktop app), not a service account."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Service account has private_key; OAuth client has "installed" or "web"
        if "private_key" in data:
            return False
        return "installed" in data or "web" in data
    except (OSError, json.JSONDecodeError):
        return False


def _get_oauth_credentials(credentials_path: str):
    """Load or obtain OAuth2 credentials (personal Google account). Uses token.json in same dir."""
    token_path = Path(credentials_path).parent / "token.json"
    creds = None
    if token_path.exists():
        try:
            creds = OAuth2Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (OSError, ValueError):
            pass
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def get_services(credentials_path: str):
    """Build Drive and Sheets API services. Uses OAuth (personal) or service account from -c file."""
    if _is_oauth_client_secrets(credentials_path):
        creds = _get_oauth_credentials(credentials_path)
    else:
        creds = ServiceAccountCredentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets


def get_spreadsheet_title(sheets_service, spreadsheet_id: str) -> str:
    """Get the spreadsheet title."""
    meta = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="properties/title")
        .execute()
    )
    return (meta.get("properties") or {}).get("title", "Untitled").strip() or "Untitled"


def get_sheet_titles(sheets_service, spreadsheet_id: str) -> list[str]:
    """Return list of sheet (tab) titles."""
    meta = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))")
        .execute()
    )
    return [
        s["properties"]["title"]
        for s in meta.get("sheets", [])
    ]


def get_formulas_for_sheet(sheets_service, spreadsheet_id: str, sheet_title: str) -> list[list] | None:
    """Get cell values with formulas (formula text, not evaluated). Returns 2D list or None on error."""
    safe = sheet_title if re.match(r"^[A-Za-z0-9_]+$", sheet_title) else f"'{sheet_title}'"
    try:
        result = (
            sheets_service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=safe,
                valueRenderOption="FORMULA",
            )
            .execute()
        )
        return result.get("values") or []
    except Exception:
        return None


def collect_all_referenced_ids(sheets_service, spreadsheet_id: str) -> set[str]:
    """Collect all spreadsheet IDs referenced by formulas in this spreadsheet."""
    refs = set()
    for title in get_sheet_titles(sheets_service, spreadsheet_id):
        grid = get_formulas_for_sheet(sheets_service, spreadsheet_id, title)
        if not grid:
            continue
        for row in grid:
            for cell in row:
                if isinstance(cell, str) and cell.strip().startswith("="):
                    refs |= spreadsheet_ids_in_formula(cell)
    return refs


def collect_references_and_warn(
    sheets_service,
    spreadsheet_ids: list[str],
) -> dict[str, set[str]]:
    """
    For each spreadsheet, collect referenced spreadsheet IDs.
    Returns refs_by_sheet: spreadsheet_id -> set(referenced_ids).
    Prints warnings for references to sheets not in spreadsheet_ids.
    """
    refs_by_sheet = {}
    all_referenced = set()
    for sid in spreadsheet_ids:
        refs = collect_all_referenced_ids(sheets_service, sid)
        refs_by_sheet[sid] = refs
        all_referenced |= refs

    snapshot_set = set(spreadsheet_ids)
    external = all_referenced - snapshot_set
    # Remove self-references
    external = {e for e in external if e not in snapshot_set}

    if external:
        logger.warning(
            "The following spreadsheet(s) are referenced in formulas but are NOT in your snapshot list. "
            "If those files are updated later, your PIT copies will show new values (value innovation). "
            "Recommended: add these IDs to your snapshot list and run again."
        )
        for eid in sorted(external):
            logger.warning("  - %s", eid)

    return refs_by_sheet


def create_drive_folder(drive_service, name: str, parent_id: str | None = None) -> str:
    """Create a folder in Drive. Returns folder ID."""
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    f = drive_service.files().create(body=body, fields="id").execute()
    return f["id"]


def copy_spreadsheet_in_drive(
    drive_service,
    spreadsheet_id: str,
    new_name: str,
    parent_id: str,
) -> str:
    """Copy a spreadsheet in Drive. Returns the new file (spreadsheet) ID."""
    body = {"name": new_name, "parents": [parent_id]}
    new_file = (
        drive_service.files()
        .copy(fileId=spreadsheet_id, body=body, fields="id")
        .execute()
    )
    return new_file["id"]


def _formula_references_other_tab(formula: str) -> bool:
    """True if formula looks like a same-spreadsheet reference to another tab (e.g. ='Sheet'!A1)."""
    if not formula or not formula.strip().startswith("="):
        return False
    # Same-doc ref: quoted sheet name + ! (and no IMPORTRANGE / external URL)
    if "IMPORTRANGE" in formula.upper() or "/d/" in formula:
        return False
    return "'" in formula and "!" in formula


def rewrite_formulas_in_copy(
    sheets_service,
    copy_spreadsheet_id: str,
    id_mapping: dict[str, str],
) -> None:
    """
    For the given (already copied) spreadsheet, get all formula cells,
    replace referenced spreadsheet IDs with PIT copy IDs, and write back.
    Also re-writes same-spreadsheet tab references (e.g. ='PnL (linked)'!B7) unchanged
    to force Sheets to re-resolve them after copy (Drive copy can break internal gid refs).
    """
    sheet_titles = get_sheet_titles(sheets_service, copy_spreadsheet_id)
    updates = []  # list of {"range": "Sheet1!A1", "values": [[formula]]}

    for sheet_title in sheet_titles:
        grid = get_formulas_for_sheet(sheets_service, copy_spreadsheet_id, sheet_title)
        if not grid:
            continue
        for row_idx, row in enumerate(grid):
            for col_idx, cell in enumerate(row):
                if not isinstance(cell, str) or not cell.strip().startswith("="):
                    continue
                formula = cell.strip()
                range_a1 = a1_notation(sheet_title, row_idx, col_idx)

                # Cross-spreadsheet refs: rewrite to PIT copy IDs
                refs = spreadsheet_ids_in_formula(formula)
                if refs.intersection(id_mapping.keys()):
                    new_formula = replace_spreadsheet_refs_in_formula(formula, id_mapping)
                    if new_formula != formula:
                        updates.append({"range": range_a1, "values": [[new_formula]]})
                        continue

                # Same-spreadsheet tab refs: write back unchanged to force re-resolve after copy
                # (Drive copy can change internal sheet IDs and break refs like ='PnL (linked)'!B7)
                if _formula_references_other_tab(formula):
                    updates.append({"range": range_a1, "values": [[formula]]})

    if not updates:
        return
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": updates,
    }
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=copy_spreadsheet_id,
        body=body,
    ).execute()


def run_pit_snapshot(
    drive_service,
    sheets_service,
    spreadsheet_ids: list[str],
    timestamp: str,
    output_folder_id: str | None,
    destination_folder_id: str | None,
) -> dict[str, str]:
    """
    Copy each spreadsheet to Drive and rewrite cross-references.
    - If destination_folder_id: create a PIT-named subfolder inside it, put all copies there.
    - If output_folder_id: put all copies directly in that folder.
    - Else: create "PIT Snapshots ..." in Drive root.
    Returns mapping: original_id -> copy_id.
    """
    id_to_copy = {}
    folder_name = f"PIT Snapshots {timestamp.replace('_', ' ')}"
    if destination_folder_id:
        target_parent = create_drive_folder(
            drive_service, folder_name, parent_id=destination_folder_id
        )
        logger.info("Created PIT subfolder: %s (ID: %s)", folder_name, target_parent)
    elif output_folder_id:
        target_parent = output_folder_id
    else:
        target_parent = create_drive_folder(drive_service, folder_name)
        logger.info("Created Drive folder: %s (ID: %s)", folder_name, target_parent)

    for sid in spreadsheet_ids:
        title = get_spreadsheet_title(sheets_service, sid)
        copy_name = f"{title} (PIT {timestamp})"
        copy_id = copy_spreadsheet_in_drive(
            drive_service, sid, copy_name, target_parent
        )
        id_to_copy[sid] = copy_id
        logger.info("Copy: %s -> %s (ID: %s)", title, copy_name, copy_id)

    # 2) Rewrite formulas in each copy so refs point to PIT copies
    for sid, copy_id in id_to_copy.items():
        rewrite_formulas_in_copy(sheets_service, copy_id, id_to_copy)
    return id_to_copy


def test_connection(credentials_path: str) -> int:
    """
    Verify credentials and API access. For OAuth, may open browser on first run.
    Returns 0 on success, 1 on failure.
    """
    logger.info("Using credentials: %s", credentials_path)
    if _is_oauth_client_secrets(credentials_path):
        logger.info(
            "Detected OAuth client secrets (personal account). First run may open a browser to sign in."
        )
    else:
        logger.info("Detected service account credentials.")
    logger.info("Connecting...")
    try:
        drive_service, sheets_service = get_services(credentials_path)
    except Exception as e:
        logger.error("Failed to load credentials: %s", e)
        return 1
    try:
        about = drive_service.about().get(fields="user").execute()
        user = about.get("user") or {}
        email = user.get("emailAddress", "(unknown)")
        logger.info("Connected to Google Drive as: %s", email)
    except Exception as e:
        logger.error("Drive API error: %s", e)
        return 1
    try:
        logger.info("Sheets API: scopes accepted (will work when you pass a spreadsheet ID).")
    except Exception as e:
        logger.error("Sheets API error: %s", e)
        return 1
    logger.info("Connection test passed. You can run a snapshot with a spreadsheet ID.")
    return 0


@click.command()
@click.argument("sheets", nargs=-1, metavar="[SHEET_ID_OR_URL]...")
@click.option("-c", "--credentials", type=click.Path(path_type=Path), default="credentials.json", show_default=True, envvar="GOOGLE_APPLICATION_CREDENTIALS", help="Path to credentials JSON.")
@click.option("-o", "--output-folder-id", metavar="ID", default="", envvar="PIT_OUTPUT_FOLDER_ID", help="Drive folder ID; copies go directly here.")
@click.option("-d", "--destination-folder", metavar="ID", default="", envvar="PIT_DESTINATION_FOLDER_ID", help="Drive folder ID; PIT subfolder created inside.")
@click.option("-t", "--timestamp", default=None, help="Timestamp for copy names.")
@click.option("--no-warn-external", is_flag=True, help="Do not warn about external refs.")
@click.option("--test", is_flag=True, help="Only test connectivity.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def cli(sheets, credentials, output_folder_id, destination_folder, timestamp, no_warn_external, test, verbose):
    """Create PIT copies of Google Sheets in Drive. Cross-references rewritten to PIT copies. Pass sheet IDs or set SHEET_IDS."""
    _configure_logging(verbose=verbose)
    cred_path = str(credentials)
    if not Path(cred_path).is_file():
        logger.error("Credentials file not found: %s", cred_path)
        raise SystemExit(1)

    if test:
        raise SystemExit(test_connection(cred_path))

    raw = list(sheets) or os.environ.get("SHEET_IDS", "").strip().split()
    if not raw:
        logger.error("Provide at least one spreadsheet ID or URL, or set SHEET_IDS.")
        raise SystemExit(1)
    spreadsheet_ids = []
    for r in raw:
        try:
            spreadsheet_ids.append(extract_sheet_id(r))
        except ValueError as e:
            logger.warning("Skipping invalid entry: %s", e)
    if not spreadsheet_ids:
        logger.error("No valid spreadsheet IDs.")
        raise SystemExit(1)

    ts = timestamp or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    drive_service, sheets_service = get_services(cred_path)

    if not no_warn_external:
        collect_references_and_warn(sheets_service, spreadsheet_ids)

    logger.info("Creating PIT copies in Google Drive...")
    id_mapping = run_pit_snapshot(
        drive_service,
        sheets_service,
        spreadsheet_ids,
        ts,
        output_folder_id=output_folder_id or None,
        destination_folder_id=destination_folder or None,
    )
    logger.info("Done. %s spreadsheet(s) copied; cross-references updated.", len(id_mapping))
    for orig, copy_id in id_mapping.items():
        logger.info("  %s -> %s", orig, copy_id)



if __name__ == "__main__":
    cli()
