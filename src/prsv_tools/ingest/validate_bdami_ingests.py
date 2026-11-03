import argparse
import os
import sys
import logging
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path
import shutil
import requests
import sqlite3
import time
import json
import csv
import xml.etree.ElementTree as ET

import prsv_tools.manage.create_pkg_report as create_pkg_report
import prsv_tools.utility.api as prsvapi
import prsv_tools.utility.cli as prsvcli
from prsv_tools.utility.get_md5 import calculate_md5
import prsv_tools.ingest.move_ami_linted_issues as mv_issues
import prsv_tools.manage.prsv_move as prsv_move
import prsv_tools.utility.fuzzy_match as fuzzy_match

TOKEN_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
THREAD_LOCAL = threading.local()

IGNORED_FILES = {
    ".ds_store", "premis-events.json", "thumbs.db",
    "manifest-md5.txt", "tagmanifest-md5.txt", "bagit.txt", "bag-info.txt", "bag-info.txt.biltemp.biltemp"
}
ALLOWED_MISSING_FROM_PRSV = {
    "bad-service-copies-note.rtf"
}

BDAMI_UUID_PROD = "3e70b062-6e58-4dcd-84e8-e24194f3467d"
BDAMI_UUID_TEST = "ec66e1e0-ac76-4c1a-a90f-417f82f9cfab"

def get_thread_session():
    """Returns a dedicated requests.Session for the calling thread."""
    if not hasattr(THREAD_LOCAL, "session"):
        THREAD_LOCAL.session = create_pkg_report.requests_retry_session()
    return THREAD_LOCAL.session

def setup_logging(log_file: Path, is_verbose: bool):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        logger.handlers.clear()

    # logfile handler
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(str(log_file), mode='a')
    fh.setFormatter(log_file_formatter)
    logger.addHandler(fh)

    # console handler
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(console_formatter)
    logger.addHandler(ch)

    # list_logger
    basic_formatter = logging.Formatter('%(message)s')

    list_logger = logging.getLogger('list_logger')
    list_logger.setLevel(logging.INFO)
    list_logger.propagate = False
    if list_logger.hasHandlers():
        list_logger.handlers.clear()
        
    bh = logging.StreamHandler(sys.stdout)
    bh.setFormatter(basic_formatter)
    list_logger.addHandler(bh)

    lh = logging.FileHandler(str(log_file), mode='a')
    lh.setFormatter(basic_formatter)
    list_logger.addHandler(lh)

    manifest_logger = logging.getLogger('manifest_logger')
    manifest_logger.setLevel(logging.INFO)
    manifest_logger.propagate = False
    if manifest_logger.hasHandlers():
        manifest_logger.handlers.clear()

    mh = logging.FileHandler(str(log_file), mode='a')
    mh.setFormatter(basic_formatter)
    manifest_logger.addHandler(mh)

    if is_verbose:
        mch = logging.StreamHandler(sys.stdout)
        mch.setFormatter(basic_formatter)
        manifest_logger.addHandler(mch)
    
    return logger, list_logger, manifest_logger


def parse_args():
    # to gracefully skip empty directories
    filtered_argv = []
    is_directory_arg = False
    
    for arg in sys.argv:
        if arg in ['-d', '--directory']:
            is_directory_arg = True
            filtered_argv.append(arg)
            continue
        if arg.startswith('-') and not arg.startswith('--directory='):
            is_directory_arg = False

        if is_directory_arg and not arg.startswith('-'):
            dir_path = Path(arg)
            if dir_path.is_dir():
                try:
                    has_children = any(child.is_dir() for child in dir_path.iterdir())
                    if not has_children:
                        print(f"WARNING: Skipping '{dir_path}' (does not contain child directories).")
                        sys.exit()
                except Exception as e:
                    print(f"WARNING: Skipping '{dir_path}' (Error checking contents: {e}).")
                    sys.exit()
        
        elif arg.startswith("--directory="):
            dir_path = Path(arg.split("=", 1)[1])
            if dir_path.is_dir():
                try:
                    has_children = any(child.is_dir() for child in dir_path.iterdir())
                    if not has_children:
                        print(f"WARNING: Skipping '{dir_path}' (does not contain child directories).")
                        continue
                except Exception as e:
                    print(f"WARNING: Skipping '{dir_path}' (Error checking contents: {e}).")
                    continue

        filtered_argv.append(arg)

    if len(filtered_argv) > 0 and filtered_argv[-1] in ['-d', '--directory']:
        filtered_argv.pop()
    for i in range(len(filtered_argv) - 1, 0, -1):
        if filtered_argv[i-1] in ['-d', '--directory'] and filtered_argv[i].startswith('-'):
            filtered_argv.pop(i-1)

    sys.argv = filtered_argv

    parser = prsvcli.Parser()
    parser.add_package()
    parser.add_packagedirectory()
    parser.add_argument(
        "--destination",
        type=Path,
        help="[Optional] Path to sort valid & invalid packages to. (Top level directories containing multiple packages)"
    )
    parser.add_argument(
        "--credentials", "-c",
        required=True,
        help="Credentials set name to use (default: prod-ingest)."
    )
    parser.add_argument(
        "--log_path", "-l",
        required=False,
        type=Path,
        help="Path to save the log file."
    )
    parser.add_argument(
        "--verbose", "-v",
        required=False,
        action='store_true',
        help="Print verbose output."
    )
    parser.add_argument(
        "--threads", "-t",
        required=False,
        type=int,
        default=5,
        help="Number of concurrent threads to use (default: 5)."
    )
    parser.add_argument(
        "--deletion-parent-ref", "-dpf",
        type=str,
        required=False,
        help="[Optional] The parent ref to move packages to if they fail validation."
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        required=False,
        help="[Optional] Skip the database update."
    )
    parser.add_argument(
        "--icacheck",
        action="store_true",
        required=False,
        help="[Optional] Check the opex_AMI folder for packages not found in Preservica (packages in queue for ingest)."
    )
    # parser.add_argument(
        # "--batchname",
        # type=str,
        # required=True,
        # help="Name of batch package was ingested with/has been grouped with [temporary required arg]."
    # )
    return parser.parse_args()

def get_auth_token(credentials_name):
    with TOKEN_LOCK:
        token = prsvapi.get_token(credentials_name)
        version = prsvapi.find_apiversion(credentials_name)
        return token, version

def refresh_auth_token(credentials_name, old_token):
    with TOKEN_LOCK:
        current_token = prsvapi.get_token(credentials_name)
        if current_token and current_token != old_token:
            version = prsvapi.find_apiversion(credentials_name)
            return current_token, version

        token_file = Path(f"{credentials_name}.token.file")
        if token_file.exists():
            try:
                token_file.unlink()
            except OSError:
                pass
        token = prsvapi.get_token(credentials_name)
        version = prsvapi.find_apiversion(credentials_name)
        return token, version

def _find_matching_dirs(root: str, dirs: list) -> dict:
    matches = {}
    for d in dirs:
        dir_path = os.path.join(root, d)
        # checking for structure rather than name (change in ami to use this instead?)
        has_manifest = os.path.isfile(os.path.join(dir_path, 'manifest-md5.txt'))
        has_data_dir = os.path.isdir(os.path.join(dir_path, 'data'))
        if has_manifest and has_data_dir:
            matches.setdefault(d, []).append(dir_path)
    return matches


def get_local_files(bdami_path, bdami_id, logger):
    """Returns dict of {filename: filesize} and {filename: full_path}."""
    files = {}
    local_paths = {}
    broken_syms = {}

    files_to_check = []
    files_to_ignore = []
    
    for root, dirs, filenames in os.walk(bdami_path, followlinks=True):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in filenames:
            if name.startswith(".") or name.endswith(".old") or name.lower() in IGNORED_FILES:
                files_to_ignore.append(name)
                continue
            
            files_to_check.append(name)
            
            full_path = Path(root) / name
            
            if full_path.is_symlink():
                resolved_target = full_path.resolve()
                if not resolved_target.exists():
                    corrected_path = str(resolved_target).replace("/source/", "/Volumes/") 
                    resolved_target = Path(corrected_path)
                
                if resolved_target.exists() and resolved_target.is_file():
                    files[name] = resolved_target.stat().st_size
                    local_paths[name] = str(resolved_target)
                else:
                    logging.error(f"Target missing or unmounted for: {name} -> {resolved_target}")
                    broken_syms[name] = str(resolved_target)
                    
            elif full_path.exists() and full_path.is_file():
                files[name] = full_path.stat().st_size
                local_paths[name] = str(full_path.absolute())
            else:
                logging.error(f"File does not exist: {full_path}")
                
    return files, local_paths, broken_syms, files_to_check, files_to_ignore


def get_local_checksums(bdami_path):
    """Parses manifest-md5.txt to create a dict of {filename: checksum}."""
    checksums = {}
    manifest_path = Path(bdami_path) / "manifest-md5.txt"
    if manifest_path.exists():
        with manifest_path.open('r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    filename = Path(parts[-1]).name
                    for part in parts:
                        if len(part) == 32 and part.isalnum():
                            checksums[filename] = part.lower()
                            break
    return checksums

def get_uuids_for_title(token, pkg_title, parent_uuid, session):
    """Searches for multiple UUIDs matching a title using the Preservica Content API."""
    query_params = {
        "q": "", 
        "fields": [
            {"name": "xip.title", "values": [pkg_title]},
            {"name": "xip.document_type", "values": ["SO"]}
        ]
    }
    query = json.dumps(query_params)
    
    url = (
        f"https://nypl.preservica.com/api/content/search-within"
        f"?q={requests.utils.quote(query)}"
        f"&parenthierarchy={parent_uuid}"
        f"&start=0"
        f"&max=100"
        f"&metadata=''" 
    )

    headers = {"Preservica-Access-Token": token}
    uuids = []
    
    try:
        res = session.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        data = res.json()

        if data.get("success") and data.get("value", {}).get("totalHits", 0) > 0:
            value = data["value"]
            object_ids = value.get("objectIds", [])
            uuids = [u[-36:] for u in object_ids]
    except Exception as e:
        logging.error(f"Error searching for {pkg_title} UUIDs: {e}")

    return uuids

def fetch_ingest_date(token, version, uuid, session) -> str:
    """Fetches the Ingest event date from the Preservica Entity API for a given structural object."""
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{uuid}/event-actions?start=0&max=100"
    headers = {"Preservica-Access-Token": token}
    
    try:
        res = session.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        
        root = ET.fromstring(res.text)
        namespaces = {'xip': f'http://preservica.com/XIP/v{version}'}
        
        for action in root.findall(".//xip:EventAction", namespaces):
            if action.get("commandType") == "command_create":
                event = action.find("xip:Event", namespaces)
                if event is not None and event.get("type") == "Ingest":
                    date_elem = event.find("xip:Date", namespaces)
                    if date_elem is not None:
                        return date_elem.text
    except Exception as e:
        logging.debug(f"Failed to get ingest date for {uuid}: {e}")
    
    return "Unknown Date"

def get_preservica_objects(token, version, package_uuid, session):
    file_data = {}
    io_titles = set()
    namespaces = {
        'xip': f'http://preservica.com/XIP/v{version}',
        'entity': f'http://preservica.com/EntityAPI/v{version}'
    }

    # ignore child SOs list
    child_so_refs = [] 
    io_info_list = []
    
    try:
        create_pkg_report.find_all_children(token, version, package_uuid, child_so_refs, io_info_list, session, namespaces)
    except Exception as e:
        raise Exception(f"Failed finding children: {e}") from e

    for io_info in io_info_list:
        io_ref = io_info['ref']
        if title := io_info.get('title'):
            io_titles.add(title)
        
        reps = create_pkg_report.get_representation_details(token, version, io_ref, session, namespaces)
        for rep in reps:
            co_refs = create_pkg_report.get_generation_details(token, version, io_ref, rep['type'], session, namespaces)
            for co_ref in co_refs:
                generations = create_pkg_report.get_generation_numbers(token, version, co_ref, session, namespaces)
                for generation in generations:
                    bitstreams = create_pkg_report.get_bitstream_details(token, version, co_ref, generation, session, namespaces)
                    for bitstream in bitstreams:
                            file_data[bitstream['filename']] = {
                                'size': bitstream.get('filesize'),
                                'md5': bitstream.get('fixity', {}).get('MD5', '').lower(),
                                'url': bitstream.get('bitstream_url'),
                                'co_ref': co_ref
                            }

    return file_data, io_titles


def categorize_files(local_filenames, prsv_filenames, preservica_io_titles):
    local_set = set(local_filenames)
    prsv_set = set(prsv_filenames)
    
    matching_files = local_set & prsv_set
    local_to_prsv_name = {f: f for f in matching_files}
    use_transcode_match = False

    for f in local_set - matching_files:
        if f.lower().endswith('.wav'):
            p_name = f[:-4] + '.flac'
            if p_name in prsv_set:
                matching_files.add(f)
                local_to_prsv_name[f] = p_name
                use_transcode_match = True
        
        if f.lower().endswith('.mov'):
            p_name = f[:-4] + '.mkv'
            if p_name in prsv_set:
                matching_files.add(f)
                local_to_prsv_name[f] = p_name
                use_transcode_match = True
                
        if f.lower().endswith('.mp4'):
            p_name = f[:-4] + '.m4a'
            if p_name in prsv_set:
                matching_files.add(f)
                local_to_prsv_name[f] = p_name
                use_transcode_match = True
                
    missing_from_local = prsv_set - set(local_to_prsv_name.values())
    missing_from_preservica = local_set - matching_files
    
    found_as_io = missing_from_preservica & preservica_io_titles
    truly_missing = missing_from_preservica - found_as_io

    return {
        "matching": matching_files,
        "missing_local": missing_from_local,
        "truly_missing": truly_missing,
        "found_as_io": found_as_io,
        "name_mapping": local_to_prsv_name,
        "transcoded": use_transcode_match
    }


def check_sizes(cats, local_files, preservica_files, local_checksums, logger, token=None, session=None, local_paths=None, credentials_name=None):
    size_mismatch = []
    zero_byte_files = []
    checksum_mismatch = []
    fuzzy_results = {}
    recalculated_matches = set()

    for fname in cats["matching"]:
        p_name = cats["name_mapping"][fname]
        local_size = local_files[fname]
        prsv_file_info = preservica_files.get(p_name, {})

        prsv_size = prsv_file_info.get('size', -1)
        prsv_md5 = prsv_file_info.get('md5')
        local_md5 = local_checksums.get(fname)

        if not local_md5 and local_paths and fname in local_paths:
            try:
                local_md5 = calculate_md5(Path(local_paths[fname]))
                local_checksums[fname] = local_md5
            except Exception as e:
                logger.error(f"Failed to calculate missing MD5 for {fname}: {e}")

        if prsv_size == 0:
            zero_byte_files.append(fname)
            continue
            
        is_transcoded_file = (
            (fname.lower().endswith('.wav') and p_name.lower().endswith('.flac')) or
            (fname.lower().endswith('.mov') and p_name.lower().endswith('.mkv')) or
            (fname.lower().endswith('.mp4') and p_name.lower().endswith('.m4a'))
        )

        if not is_transcoded_file:
            size_mismatch_found = local_size != prsv_size
            checksum_mismatch_found = local_md5 and prsv_md5 and local_md5 != prsv_md5

            if size_mismatch_found or checksum_mismatch_found:
                if fname.lower().endswith('.json'):
                    co_ref = prsv_file_info.get('co_ref')
                    if co_ref and local_paths and fname in local_paths:
                        local_json_path = local_paths[fname]
                        try:
                            with open(local_json_path, "r", encoding="utf-8-sig") as f:
                                local_data = json.load(f)
                            logger.info(f"Successfully read local JSON: {fname}")

                            if token and session:
                                content_url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations/latest-active/bitstreams/1/content"
                                headers = {
                                    "Preservica-Access-Token": token,
                                    "Accept": "application/octet-stream"
                                }
                                try:
                                    response = session.get(content_url, headers=headers, timeout=(60, 60))
                                    response.raise_for_status()
                                except requests.exceptions.HTTPError as err:
                                    if err.response.status_code == 401 and credentials_name:
                                        logger.warning(f"401 Unauthorized for {fname}. Refreshing token and retrying download...")
                                        new_token, _ = refresh_auth_token(credentials_name, token)
                                        headers["Preservica-Access-Token"] = new_token
                                        response = session.get(content_url, headers=headers, timeout=(60, 60))
                                        response.raise_for_status()
                                    else:
                                        raise
                                prsv_data = json.loads(response.content.decode("utf-8-sig"))

                                try:
                                    diffs, warnings = fuzzy_match.fuzzy_compare(local_data, prsv_data)
                                except Exception as e:
                                    logger.error(f"Failed to compare JSON for {fname}: {e}")
                                    diffs, warnings = [], []
                                        
                                if diffs:
                                    logger.error(f"JSON files do NOT match for {fname}. Found {len(diffs)} difference(s):")
                                    for d in diffs:
                                        logger.error(f" - {d}")
                                        
                                    all_diffs_acceptable = False

                                    is_missing_keys_in_prsv = any("Keys missing in second file" in d for d in diffs)
                                    prsv_is_later = False
                                    
                                    try:
                                        l_date_str = local_data.get('technical', {}).get('dateCreated')
                                        p_date_str = prsv_data.get('technical', {}).get('dateCreated')

                                        if l_date_str and p_date_str:
                                            try:
                                                l_date = datetime.fromisoformat(l_date_str.replace("Z", "+00:00"))
                                                p_date = datetime.fromisoformat(p_date_str.replace("Z", "+00:00"))
                                                prsv_is_later = p_date > l_date
                                            except ValueError:
                                                prsv_is_later = p_date_str > l_date_str
                                    except Exception as e:
                                        logger.debug(f"Could not parse 'dateCreated' for {fname}: {e}")

                                    if prsv_is_later and not is_missing_keys_in_prsv:
                                        all_diffs_acceptable = True
                                        logger.info(f"JSON logically matches for {fname} (Preservica 'dateCreated' is newer and no keys missing). Ignoring strict mismatch.")
                                    
                                    else:
                                        is_bwf_to_flac = any("technical.fileFormat: 'BWF' vs 'FLAC'" in d for d in diffs)
                                        
                                        if is_bwf_to_flac:
                                            all_diffs_acceptable = True
                                            for d in diffs:
                                                if "technical.fileFormat: 'BWF' vs 'FLAC'" in d:
                                                    continue
                                                elif "technical.audioCodec: 'PCM' vs 'FLAC'" in d:
                                                    continue
                                                    
                                                elif "technical.durationMilli.measure:" in d:
                                                    try:
                                                        val_str = d.split("durationMilli.measure:")[-1].strip()
                                                        v1, v2 = val_str.split(" vs ")
                                                        if float(v2) >= float(v1):
                                                            continue
                                                    except Exception:
                                                        pass
                                                    all_diffs_acceptable = False
                                                    break
                                                    
                                                elif "technical.durationHuman:" in d:
                                                    try:
                                                        val_str = d.split("durationHuman:")[-1].strip()
                                                        v1, v2 = [v.strip().strip("'") for v in val_str.split(" vs ")]
                                                        if v2.startswith(v1):
                                                            continue
                                                    except Exception:
                                                        pass
                                                    all_diffs_acceptable = False
                                                    break
                                                    
                                                else:
                                                    all_diffs_acceptable = False
                                                    break
                                                    
                                    if all_diffs_acceptable:
                                        fuzzy_results[fname] = True
                                        size_mismatch_found = False
                                        checksum_mismatch_found = False
                                    else:
                                        fuzzy_results[fname] = False
                                        
                                        if checksum_mismatch_found:
                                            logger.warning(f"Fuzzy match failed for {fname}. Recalculating local MD5...")
                                            new_md5 = calculate_md5(Path(local_paths[fname]))
                                            if new_md5 == prsv_md5:
                                                logger.warning(f"Recalculated MD5 for {fname} matches Preservica. Marking as valid.")
                                                recalculated_matches.add(fname)
                                                checksum_mismatch_found = False
                                            else:
                                                logger.error(f"Recalculated MD5 for {fname} ({new_md5}) still does not match Preservica ({prsv_md5})")
                                else:
                                    size_mismatch_found = False
                                    checksum_mismatch_found = False
                                    fuzzy_results[fname] = True
                                    logger.info(f"JSON logically matches for {fname}. Ignoring strict checksum/size mismatch.")

                        except Exception as e:
                            logger.error(f"Failed to read local or download Preservica JSON for {fname}: {e}")
                            
                            if checksum_mismatch_found:
                                logger.warning(f"Fuzzy match error for {fname}. Falling back to MD5 recalculation...")
                                new_md5 = calculate_md5(Path(local_paths[fname]))
                                if new_md5 == prsv_md5:
                                    logger.warning(f"Recalculated MD5 for {fname} matches Preservica. Marking as valid.")
                                    recalculated_matches.add(fname)
                                    checksum_mismatch_found = False
                                else:
                                    logger.error(f"Recalculated MD5 for {fname} ({new_md5}) still does not match Preservica ({prsv_md5})")

                else:
                    if checksum_mismatch_found and local_paths and fname in local_paths:
                        logger.warning(f"MD5 mismatch for {fname} (Manifest: {local_md5}, Prsv: {prsv_md5}). Recalculating local MD5...")
                        new_md5 = calculate_md5(Path(local_paths[fname]))
                        if new_md5 == prsv_md5:
                            logger.warning(f"Recalculated MD5 for {fname} matches Preservica. Marking as valid.")
                            recalculated_matches.add(fname)
                            checksum_mismatch_found = False
                        else:
                            logger.error(f"Recalculated MD5 for {fname} ({new_md5}) still does not match Preservica ({prsv_md5})")

                if size_mismatch_found:
                        size_mismatch.append((fname, local_size, prsv_size))

                if checksum_mismatch_found:
                    checksum_mismatch.append((fname, local_md5, prsv_md5))

    return zero_byte_files, size_mismatch, checksum_mismatch, fuzzy_results, recalculated_matches


def log_verbose_manifest(bdami_id, cats, local_files, preservica_files, local_checksums, manifest_logger, files_checked, files_ignored, recalculated_matches=None, fuzzy_results=None):
    if recalculated_matches is None:
        recalculated_matches = set()
    if fuzzy_results is None:
        fuzzy_results = {}
    with LOG_LOCK:
        manifest_logger.info(f"Checking package: {bdami_id}")
        manifest_logger.info(f"Checking: {', '.join(files_checked)}")
        manifest_logger.info(f"Ignoring: {', '.join(files_ignored) if files_ignored else '--'}")
        header = f"{'LOCAL':<55} {'PRESERVICA STATUS'}"
        separator = f"{'-'*55} {'-'*40}"
        output = [f"\n{bdami_id} VERBOSE MANIFEST:", header, separator]
        print_files = set(local_files.keys()).union(cats["missing_local"])
        
        for f in sorted(print_files):
            if f in cats["matching"]:
                p_name = cats["name_mapping"][f]
                l_size = local_files[f]
                p_info = preservica_files.get(p_name, {})
                p_size = p_info.get('size')
                l_md5 = local_checksums.get(f)
                p_md5 = p_info.get('md5')
                
                if f != p_name:
                    if f.lower().endswith('.wav'):
                        status_msg = f"{p_name} [WAV TO FLAC MATCH]"
                    elif f.lower().endswith('.mov'):
                        status_msg = f"{p_name} [MOV TO MKV MATCH]"
                    elif f.lower().endswith('.mp4'):
                        status_msg = f"{p_name} [MP4 TO M4A MATCH]"
                    else:
                        status_msg = f"{p_name} [TRANSCODED FILE MATCH]"
                elif p_size == 0:
                    status_msg = f"{f} [0 BYTES IN PRESERVICA]"
                elif l_size != p_size:
                    status_msg = f"{f} [SIZE MISMATCH: L={l_size} vs P={p_size}]"
                elif l_md5 and p_md5 and l_md5 != p_md5:
                    if f in recalculated_matches:
                        status_msg = f"{f} [MD5 in local manifest-md5.txt was incorrect but calculated local MD5 matches Preservica]"
                    else:
                        status_msg = f"{f} [MD5 MISMATCH: L={l_md5[:6]}... vs P={p_md5[:6]}...]"
                else:
                    md5_str = "MD5 MATCH" if l_md5 and p_md5 else "NO MD5"
                    status_msg = f"{f} ({l_size} bytes | {md5_str})"
                
                if f in fuzzy_results:
                    if fuzzy_results[f]:
                        status_msg += " [**FUZZY MATCH SUCCESSFUL**]"
                    else:
                        status_msg += " [**FUZZY MATCH FAILED**]"
                
                output.append(f"{f:<55} --------> {status_msg}")
                
            elif f in cats["found_as_io"]:
                output.append(f"{f:<55} --------> [FOUND AS IO TITLE ONLY]")
            elif f in cats["truly_missing"]:
                output.append(f"{f:<55} --------> [TRULY MISSING IN PRESERVICA]")
            elif f in cats["missing_local"]:
                output.append(f"{'[MISSING LOCALLY]':<55} --------> {f}")
        
        output.append("")
        manifest_logger.info("\n".join(output))


def move_pkgs(source_path: Path, destination_path: Path):
    try:
        parent_name = source_path.parent.name
        if parent_name in {"Audio", "Film", "Video"}:
            target_path = destination_path / source_path.parent.parent.name / source_path.name
        else:
            target_path = destination_path / parent_name / source_path.name

        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(target_path))
        logging.info(f"Moved '{source_path}' to '{target_path}'")
    except Exception as e:
        logging.error(f"Error moving '{source_path}' to '{destination_path}': {e}")

def setup_database(db_path: Path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS validations (
            id TEXT PRIMARY KEY,
            batch TEXT,
            source_path TEXT,
            validation_date TEXT,
            result TEXT,
            fail_reason TEXT,
            log_name TEXT,
            type TEXT
        )
    ''')
    
    try:
        cursor.execute("ALTER TABLE validations ADD COLUMN type TEXT")
    except sqlite3.OperationalError:
        pass # column already exists
        
    conn.commit()
    return conn

def update_database(conn, bdami_id, source_path, validation_date, result, reason, log_name, pkg_type):
    cursor = conn.cursor()
    batch_name = (f"{Path(source_path).parent.parent.name}_{Path(source_path).parent.name}") if any(x in Path(source_path).parent.name for x in ["Film", "Audio", "Data", "Video"]) else (Path(source_path).parent.name)
    cursor.execute('''
        INSERT OR REPLACE INTO validations (id, batch, source_path, validation_date, result, fail_reason, log_name, type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (bdami_id, batch_name, source_path, validation_date, result, reason, log_name, pkg_type))
    conn.commit()


def validate_package(bdami_id, bdami_paths, credentials_name, parent_uuid, logger, list_logger, manifest_logger, is_previously_valid=False):
    DELETION_SEARCH_UUID = "836a114b-839a-4af8-a4e1-001f200d6d40"
    TRANSIENT_SEARCH_UUID = "380c6d78-0a8a-4843-b472-2199ba7fad72"
    
    session = get_thread_session()
    
    if is_previously_valid:
        try:
            token, version = get_auth_token(credentials_name)
            digbdami_uuids = get_uuids_for_title(token, bdami_id, parent_uuid, session)
            deletion_uuids = get_uuids_for_title(token, bdami_id, DELETION_SEARCH_UUID, session)
            transient_uuids = get_uuids_for_title(token, bdami_id, TRANSIENT_SEARCH_UUID, session)
            all_insts = [(u, "DigBDAMI") for u in digbdami_uuids] + [(u, "Deletion Folder") for u in deletion_uuids] + [(u, "Transient Folders") for u in transient_uuids]
            
            if len(all_insts) == 1:
                logger.info(f"Skipping {bdami_id}: Marked 'valid' in database and exactly 1 instance found.")
                uuid, loc = all_insts[0]
                ingest_date = fetch_ingest_date(token, version, uuid, session)
                in_deletion = bool(deletion_uuids and not digbdami_uuids)
                return True, {}, "Valid in DB (No Duplicates)", in_deletion, [{"uuid": uuid, "date": ingest_date, "transcoded": False, "location": loc}], []
            elif len(all_insts) > 1:
                logger.warning(f"{bdami_id} marked 'valid' in database but has {len(all_insts)} instances. Proceeding with full validation to map duplicates.")
        except Exception as e:
            logger.warning(f"Fast duplicate check failed for {bdami_id}: {e}. Falling back to full validation.")
            
    in_deletion_folder = False
    broken_syms = {}
    
    bdami_path = Path(bdami_paths[0])
    
    if len(bdami_paths) > 1:
        if Path(bdami_paths[0]).name == Path(bdami_paths[1]).name:
            bdami_path = Path(bdami_paths[1])
            logger.warning(f"Multiple local folders found for {bdami_id}, checking content of: {bdami_paths[1]}\n   -- All paths: {', '.join(bdami_paths)}")
        else:
            logger.warning(f"Multiple local folders found for {bdami_id}, checking content of: {bdami_path}\n   -- All paths: {', '.join(bdami_paths)}")

    local_files, local_paths, broken_syms, files_checked, files_ignored = get_local_files(bdami_path, bdami_id, logger)
    local_checksums = get_local_checksums(bdami_path)
    
    if not local_files:
        logger.warning(f"Skipping {bdami_id}: No local files found.")
        return False, broken_syms, "No local files found", in_deletion_folder, [], []

    api_success = False
    max_retries = 1
    
    matching_instances = []
    invalid_instances = []

    for attempt in range(max_retries + 1):
        try:
            token, version = get_auth_token(credentials_name)
            
            digbdami_uuids = get_uuids_for_title(token, bdami_id, parent_uuid, session)
            deletion_uuids = get_uuids_for_title(token, bdami_id, DELETION_SEARCH_UUID, session)
            transient_uuids = get_uuids_for_title(token, bdami_id, TRANSIENT_SEARCH_UUID, session)
            
            all_package_instances = [(u, "BDAMI") for u in digbdami_uuids] + [(u, "Deletion Folder") for u in deletion_uuids] + [(u, "Transient Folders") for u in transient_uuids]
            
            if not all_package_instances:
                logger.error(f"{bdami_id} not found in Preservica")
                return False, broken_syms, "Not found in Preservica", in_deletion_folder, [], []
                
            in_deletion_folder = bool(deletion_uuids and not digbdami_uuids)
            in_transient_folder = bool(len(transient_uuids)>1)

            if in_deletion_folder:
                deletion_folder_name = create_pkg_report.get_parent_so_title(token, deletion_uuids[0], version, session)
                logger.warning(f"{bdami_id} found in Deletion Folder: '{deletion_folder_name}'")

            if in_transient_folder:
                logger.warning(f"{bdami_id} instance found in Transient Folder.")

            if len(all_package_instances) > 1:
                logger.warning(f"Multiple Preservica instances found for {bdami_id}. Comparing local to ALL instances.")

            for uuid, location in all_package_instances:
                ingest_date = fetch_ingest_date(token, version, uuid, session)
                
                if location == "Transient Folders":
                    invalid_instances.append({
                        "uuid": uuid,
                        "date": ingest_date,
                        "reasons": "Located in Transient Folders",
                        "location": location
                    })
                    continue

                preservica_files, preservica_io_titles = get_preservica_objects(token, version, uuid, session)

                cats = categorize_files(local_files.keys(), preservica_files.keys(), preservica_io_titles)

                zero_byte_files, size_mismatch, checksum_mismatch, fuzzy_results, recalculated_matches = check_sizes(
                    cats, local_files, preservica_files, local_checksums, logger, token, session, local_paths, credentials_name
                )
                
                is_valid = True
                reasons = []

                truly_missing_copy = set(cats["truly_missing"])
                for missing_file in truly_missing_copy:
                    if missing_file.lower() in ALLOWED_MISSING_FROM_PRSV:
                        logger.warning(f"Package {bdami_id}: '{missing_file}' found locally but missing from Preservica. Package will not be marked invalid for this.")
                        cats["truly_missing"].remove(missing_file)

                if cats["truly_missing"]:
                    is_valid = False
                    missing_files = ", ".join(sorted(cats['truly_missing']))
                    reasons.append(f"Missing {len(cats['truly_missing'])} files from Preservica ({missing_files})")
                
                if cats["missing_local"]:
                    missing_local = ", ".join(sorted(cats['missing_local']))
                    logger.warning(f"Package {bdami_id}: Missing {len(cats['missing_local'])} files from Local ({missing_local}). Package will not be marked invalid for this.")

                if cats["found_as_io"]:
                    is_valid = False
                    io_files = ", ".join(sorted(cats['found_as_io']))
                    reasons.append(f"Bitstream mismatch - IO Title match only ({io_files})")
                    
                if zero_byte_files:
                    is_valid = False
                    zero_files = ", ".join(sorted(zero_byte_files))
                    reasons.append(f"0-byte files ({zero_files})")
                if size_mismatch:
                    is_valid = False
                    reasons.append("Size mismatch")
                if checksum_mismatch:
                    is_valid = False
                    reasons.append("Checksum mismatch")

                if (is_valid and not matching_instances) or (not is_valid and not matching_instances and not invalid_instances):
                    log_verbose_manifest(
                        bdami_id, cats, local_files, preservica_files, local_checksums, manifest_logger,
                        files_checked, files_ignored, recalculated_matches, fuzzy_results
                    )

                if is_valid:
                    matching_instances.append({"uuid": uuid, "date": ingest_date, "transcoded": cats["transcoded"], "location": location})
                else:
                    invalid_instances.append({"uuid": uuid, "date": ingest_date, "reasons": ", ".join(reasons), "location": location})

            api_success = True
            break
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401 and attempt < max_retries:
                logger.warning(f"401 error for {bdami_id}. Refreshing token and retrying package...")
                refresh_auth_token(credentials_name, token)
                continue
            logger.error(f"HTTP Error retrieving {bdami_id}: {e}")
            return False, broken_syms, "HTTP Error retrieving files", in_deletion_folder, [], []
        except Exception as e:
            if '401' in str(e) and attempt < max_retries:
                logger.warning(f"401 error for {bdami_id}. Refreshing token and retrying package...")
                refresh_auth_token(credentials_name, token)
                continue
            logger.error(f"Error retrieving files for {bdami_id}: {e}")
            return False, broken_syms, "Error retrieving files from Preservica", in_deletion_folder, [], []

    if not api_success:
        return False, broken_syms, "Failed to connect to Preservica", in_deletion_folder, [], []

    if len(all_package_instances) > 1:
        valid_digbdami_count = sum(1 for inst in matching_instances if inst['location'] == "DigBDAMI")
        valid_deletion_count = sum(1 for inst in matching_instances if inst['location'] == "Deletion Folder")
        invalids_are_deletion = all(inst['location'] == "Deletion Folder" for inst in invalid_instances)
        
        is_routine_duplicate = (valid_digbdami_count == 1 and valid_deletion_count == 0 and len(invalid_instances) > 0 and invalids_are_deletion)

        report_logger = manifest_logger if is_routine_duplicate else list_logger

        report_logger.info(f"\n--- DUPLICATE INSTANCES REPORT: {bdami_id} ---")
        if matching_instances:
            report_logger.info("MATCHING INSTANCES (Valid against local):")
            for inst in matching_instances:
                report_logger.info(f" -> UUID: {inst['uuid']} | Location: {inst['location']} | Ingested: {inst['date']}")
        if invalid_instances:
            report_logger.info("INVALID INSTANCES (Mismatch against local):")
            for inst in invalid_instances:
                report_logger.info(f" -> UUID: {inst['uuid']} | Location: {inst['location']} | Ingested: {inst['date']} | Errors: {inst['reasons']}")
        report_logger.info("--------------------------------------------\n")

    if matching_instances:
        if matching_instances[0].get("transcoded"):
            if len(matching_instances) > 1:
                logger.info(f"SUCCESS: {bdami_id} fully validated ({len(local_files)} files) - [Transcoded file matching] - ({len(matching_instances)} valid instances found).")
            else: 
                logger.info(f"SUCCESS: {bdami_id} fully validated ({len(local_files)} files) - [Transcoded file matching].")   
        else:
            if len(matching_instances) > 1:
                logger.info(f"SUCCESS: {bdami_id} fully validated ({len(local_files)} files) - ({len(matching_instances)} valid instances found).")
            else: 
                logger.info(f"SUCCESS: {bdami_id} fully validated ({len(local_files)} files).")
        return True, broken_syms, "", in_deletion_folder, matching_instances, invalid_instances
    else:
        if len(invalid_instances) > 1:
            logger.error(f"FAILED: {bdami_id} has {len(invalid_instances)} Preservica instances, but NONE match the local files.")
        else:
            logger.error(f"FAILED: {bdami_id} has a Preservica instance, but it does not match the local files.")
        
        for inst in invalid_instances:
            logger.error(f" - {inst['uuid']} failed due to: {inst['reasons']}")
            
        return False, broken_syms, "No Preservica instances matched local files", in_deletion_folder, matching_instances, invalid_instances


def main():
    args = parse_args()

    script_name = Path(__file__).stem.lower()
    if "bdami" in script_name:
        pkg_type = "BDAMI"
    elif "digarch" in script_name:
        pkg_type = "DigArch"
    else:
        pkg_type = "AMI"

    packages_list = sorted(list(args.packages))
    if not packages_list:
        sys.exit(0)
    
    first_source = packages_list[0]

    is_directory_mode = any(arg in sys.argv for arg in ['--directory', '-d'])
    initial_path = first_source.parent if is_directory_mode else first_source
    
    parts = [initial_path.name]
    current = initial_path
    categories = {"Film", "Audio", "Data", "Video"}
    
    while True:
        name = current.name
        has_bdami_structure = (current / 'manifest-md5.txt').exists() and (current / 'data').is_dir()
        is_category = name in categories and "NYPL" not in name
        
        if (has_bdami_structure or is_category) and current.parent.name not in ["", "Volumes", "/"]:
            current = current.parent
            parts.insert(0, current.name)
        else:
            break
            
    # batch_name = args.batchname
    batch_name = (initial_path).name
    log_name = f"ingest_validation_{batch_name}_{datetime.now().strftime('%Y-%m-%d')}.log"
    log_path = Path(args.log_path / log_name) if args.log_path else Path(f"logs/{log_name}")
    logger, list_logger, manifest_logger = setup_logging(log_path, args.verbose)
    
    parent_uuid = BDAMI_UUID_TEST if "test" in args.credentials else BDAMI_UUID_PROD 
    if len(packages_list) < 25:
        logger.info(f"Scanning input directories: {', '.join(str(p) for p in packages_list)}")
    else:
        logger.info(f"Scanning {len(packages_list)} input directories...")
    bdami_packages = {}
    
    for source_path in packages_list:
        if (source_path / 'manifest-md5.txt').exists() and (source_path / 'data').is_dir():
            bdami_packages.setdefault(source_path.name, []).append(str(source_path))
        
        for root, dirs, _ in os.walk(source_path, followlinks=True):
            matches = _find_matching_dirs(root, dirs)
            for bdami_id, paths in sorted(matches.items()):
                if bdami_id not in bdami_packages:
                    bdami_packages[bdami_id] = paths
                else:
                    for p in paths:
                        if p not in bdami_packages[bdami_id]:
                            bdami_packages[bdami_id].append(p)

    if not bdami_packages:
        logger.warning("No BDAMI packages found to process.")
        log_path.unlink()
        sys.exit(0)

    logger.info(f"Found {len(bdami_packages)} BDAMI packages.")

    db_path = log_path.parent.parent / "validation_db" / "validation_database.db"
    previously_valid = set()
    db_conn = None
    
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        db_conn = setup_database(db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT id FROM validations WHERE result = 'valid'")
        previously_valid = {row[0] for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"Error querying database for previously valid packages: {e}")

    try:
        get_auth_token(args.credentials)
    except Exception as e:
        logger.error(f"Fatal auth error before starting: {e}")
        log_path.unlink()
        sys.exit(1)

    deleted_pkgs = set()
    valid_ids = set()
    invalid_pkgs = []  # list of tuples: (bdami_id, path, reason)

    all_broken_syms = {}
    all_matching_instances = {}
    all_invalid_instances = {}
    
    logger.info(f"Starting validation with {args.threads} threads...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = []
        for bdami_id, paths in bdami_packages.items():
            is_prev_valid = bdami_id in previously_valid
            future = executor.submit(
                validate_package,
                bdami_id,
                paths,
                args.credentials,
                parent_uuid,
                logger,
                list_logger,
                manifest_logger,
                is_prev_valid
            )
            futures.append(future)
            
        future_to_bdami = {f: (bdami_id, paths) for f, (bdami_id, paths) in zip(futures, bdami_packages.items())}

        for future in concurrent.futures.as_completed(future_to_bdami):
            bdami_id, paths = future_to_bdami[future]
            try:
                is_valid, pkg_broken_syms, reason, in_deletion_folder, match_insts, inv_insts = future.result()

                if in_deletion_folder:
                    deleted_pkgs.add(bdami_id)
                if pkg_broken_syms:
                    all_broken_syms[bdami_id] = pkg_broken_syms
                
                if match_insts:
                    all_matching_instances[bdami_id] = match_insts
                if inv_insts:
                    all_invalid_instances[bdami_id] = inv_insts

                if is_valid:
                    valid_ids.add(bdami_id)
                else:
                    invalid_pkgs.append((bdami_id, paths[0], reason))
            except Exception as e:
                logger.error(f"Exception generated for {bdami_id}: {e}")
                if not any(pkg[0] == bdami_id for pkg in invalid_pkgs):
                    invalid_pkgs.append((bdami_id, paths[0], f"Exception during validation: {e}"))
    
    logger.info("Validation complete.")

    run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_file_name = log_path.name
 
    if not args.skip_db and db_conn:
        try:
            logging.info(f"Updating database. @ {db_path}")
            for bdami_id in valid_ids:
                src_path = str(bdami_packages[bdami_id][0])
                update_database(db_conn, bdami_id, src_path, run_time, "valid", "", log_file_name, pkg_type)
                
            for bdami_id, path, reason in invalid_pkgs:
                update_database(db_conn, bdami_id, str(path), run_time, "invalid", reason, log_file_name, pkg_type)
                
            db_conn.close()
        except Exception as e:
            logger.error(f"Failed to update database: {e}")

    if args.destination and (valid_ids or invalid_pkgs):
        destination_path = Path(args.destination)
        logger.info(f"\nMoving packages to destination: {args.destination}")
        
        for bdami_id in sorted(valid_ids):
            for source_path in bdami_packages[bdami_id]:
                move_pkgs(Path(source_path), destination_path / "_valid")

        for bdami_id, _, _ in sorted(invalid_pkgs):
            for source_path in bdami_packages[bdami_id]:
                move_pkgs(Path(source_path), destination_path / "_validation_failed")
    

    all_parents = set(Path(paths[0]).parent for paths in bdami_packages.values())
    for parent in sorted(all_parents):
        mv_issues.delete_empty_dir(parent)

    if args.deletion_parent_ref and all_invalid_instances:
        invalid_uuids = [inst['uuid'] for insts in all_invalid_instances.values() for inst in insts]
        
        logger.info(f"\nMoving {len(invalid_uuids)} invalid package instances to deletion folder in Preservica: {args.deletion_parent_ref}")
        try:
            prsv_move.process_move_list(
                credentials=args.credentials,
                uuid_list=invalid_uuids,
                new_parent_ref=args.deletion_parent_ref,
                logger=logger
            )
        except Exception as e:
            logger.error(f"Failed to execute prsv_move: {e}")

    total_pkgs = len(valid_ids) + len(invalid_pkgs)

    list_logger.info(f"\n{'='*60}")
    
    list_logger.info(f"FINAL VALIDATION SUMMARY [Batch / Package: {batch_name}]")

    summary_valid = []
    summary_invalid = []
    
    all_processed_bdamis = set(valid_ids).union({pkg[0] for pkg in invalid_pkgs})
    for bdami_id in all_processed_bdamis:
        match_insts = all_matching_instances.get(bdami_id, [])
        inv_insts = all_invalid_instances.get(bdami_id, [])
        
        valid_digbdami_count = sum(1 for inst in match_insts if inst.get('location') == "BDAMI")
        valid_deletion_count = sum(1 for inst in match_insts if inst.get('location') == "Deletion Folder")
        invalids_are_deletion = all(inst.get('location') == "Deletion Folder" for inst in inv_insts)
        
        is_routine_duplicate = (valid_digbdami_count == 1 and valid_deletion_count == 0 and len(inv_insts) > 0 and invalids_are_deletion)

        if bdami_id not in valid_ids:
            summary_invalid.append(bdami_id)
        elif len(inv_insts) > 0 and not is_routine_duplicate:
            summary_invalid.append(bdami_id)
        else:
            summary_valid.append(bdami_id)

    dupe_csv_rows = []
    duplicate_package_count = 0
    
    for bdami_id in sorted(summary_valid):
        match_insts = all_matching_instances.get(bdami_id, [])
        if len(match_insts) > 1:
            duplicate_package_count += 1
            for inst in match_insts:
                dupe_csv_rows.append([inst['uuid'], bdami_id, "VALID", inst.get('location', ''), inst.get('date', '')])
            dupe_csv_rows.append([])
                
    for bdami_id in sorted(summary_invalid):
        match_insts = all_matching_instances.get(bdami_id, [])
        inv_insts = all_invalid_instances.get(bdami_id, [])
        if (len(match_insts) + len(inv_insts)) > 1:
            duplicate_package_count += 1
            for inst in match_insts:
                dupe_csv_rows.append([inst['uuid'], bdami_id, "VALID", inst.get('location', ''), inst.get('date', '')])
            for inst in inv_insts:
                dupe_csv_rows.append([inst['uuid'], bdami_id, "INVALID", inst.get('location', ''), inst.get('date', '')])
            dupe_csv_rows.append([])

    export_dupes_to_csv = duplicate_package_count > 5
    if export_dupes_to_csv:
        csv_date = datetime.now().strftime('%Y-%m-%d')
        csv_filename = f"{batch_name}_DupeReport_{csv_date}.csv"
        csv_path = Path.home() / "Downloads" / csv_filename
        try:
            with open(csv_path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["UUID", "Package ID", "VALID/INVALID", "LOCATION", "Ingest Date"])
                writer.writerows(dupe_csv_rows)
            list_logger.info(f"\n[NOTE: {duplicate_package_count} duplicate packages found. Detailed instances exported to {csv_filename}]")
        except Exception as e:
            logger.error(f"Failed to write duplicate report CSV: {e}")
            export_dupes_to_csv = False  

    list_logger.info(f"\nVALID PACKAGES ({len(summary_valid)}/{total_pkgs}):")
    for bdami_id in sorted(summary_valid):
        match_insts = all_matching_instances.get(bdami_id, [])
        total_instances = len(match_insts)
        
        if total_instances > 1:
            if export_dupes_to_csv:
                list_logger.info(f"- {bdami_id} | [VALID] ({total_instances} Instances) -> [See CSV]")
            else:
                list_logger.info(f"- {bdami_id} | [VALID] ({total_instances} Instances)")
                for inst in match_insts:
                    list_logger.info(f"                 - UUID: {inst['uuid']} | Location: {inst['location']} | Ingest date: {inst['date']}")

    list_logger.info(f"\nINVALID PACKAGES ({len(summary_invalid)}/{total_pkgs}):")

    all_not_found = False
    if len(summary_valid) == 0 and total_pkgs > 0:
        all_reasons_not_found = all(
            next((r for a, p, r in invalid_pkgs if a == bdami_id), "") == "Not found in Preservica"
            for bdami_id in summary_invalid
        )
        if all_reasons_not_found:
            found_any_in_opex = False
            if args.icacheck:
                opex_dir = Path("/Users/emileebuytkins/VM/pvm25/ifs/preservica/production/source/opex_BDAMI/")
                if opex_dir.exists():
                    try:
                        for bdami_id in summary_invalid:
                            bdami_id_search = f"_{bdami_id}"
                            if any(bdami_id_search in f.name for f in opex_dir.iterdir()):
                                found_any_in_opex = True
                                break
                    except Exception as e:
                        logger.error(f"Failed to check opex_BDAMI for queued packages: {e}")
            if not found_any_in_opex:
                all_not_found = True

    if all_not_found:
        list_logger.info("All submitted packages are invalid, not found in Preservica.")
    else:
        for bdami_id in sorted(summary_invalid):
            match_insts = all_matching_instances.get(bdami_id, [])
            inv_insts = all_invalid_instances.get(bdami_id, [])
            total_instances = len(match_insts) + len(inv_insts)
            
            if total_instances > 1:
                if export_dupes_to_csv:
                    list_logger.info(f"- {bdami_id} | {len(match_insts)} VALID, {len(inv_insts)} INVALID ({total_instances} Instances) -> [See CSV]")
                else:
                    list_logger.info(f"- {bdami_id} | {len(match_insts)} VALID, {len(inv_insts)} INVALID ({total_instances} Instances)")
                    for inst in match_insts:
                        list_logger.info(f"                 -[VALID] UUID: {inst['uuid']} | Location: {inst['location']} | Ingest date: {inst['date']}")
                    for inst in inv_insts:
                        list_logger.info(f"                 -[INVALID] UUID: {inst['uuid']} | Location: {inst['location']} | Ingest date: {inst['date']} | Errors: {inst['reasons']}")

            else:
                reason = next((r for a, p, r in invalid_pkgs if a == bdami_id), "Unknown error")
                pkg_path = next((p for a, p, r in invalid_pkgs if a == bdami_id), bdami_packages.get(bdami_id, ["Unknown path"])[0])
                del_note = " [FOUND IN DELETION FOLDER]" if bdami_id in deleted_pkgs else ""
                
                prefix = "-"
                ica_note = ""
                if args.icacheck and reason == "Not found in Preservica":
                    opex_dir = Path("/Users/emileebuytkins/VM/pvm25/ifs/preservica/production/source/opex_BDAMI/")
                    if opex_dir.exists():
                        try:
                            bdami_id_search = f"_{bdami_id}"
                            found_items = [f.name for f in opex_dir.iterdir() if bdami_id_search in f.name]
                            if found_items:
                                prefix = "^ -"
                                ica_note = f" [FOUND IN OPEX_BDAMI: {', '.join(found_items)}]"
                        except Exception as e:
                            logger.error(f"Failed to check opex_BDAMI for {bdami_id}: {e}")

                list_logger.info(f"{prefix} {pkg_path} | (FAILED DUE TO: {reason}){del_note}{ica_note}")

    deletions_valid = set()
    deletions_invalid = set()
    invalid_ids_list = [pkg[0] for pkg in invalid_pkgs]
    invalid_ids_set = set(invalid_ids_list) 
    
    for bdami_id in deleted_pkgs:
        if bdami_id in valid_ids:
            deletions_valid.add(bdami_id)
        if bdami_id in invalid_ids_set:
            deletions_invalid.add(bdami_id)

    transient_single_invalid = set()
    for bdami_id in invalid_ids_set:
        match_insts = all_matching_instances.get(bdami_id, [])
        inv_insts = all_invalid_instances.get(bdami_id, [])
        if len(match_insts) == 0 and len(inv_insts) == 1 and inv_insts[0].get('location') == "Transient Folders":
            transient_single_invalid.add(bdami_id)

    digbdami_valid_ids = [bdami_id for bdami_id in valid_ids if (bdami_id not in deletions_valid)]
    digbdami_invalid_ids = [bdami_id for bdami_id, _, reason in invalid_pkgs if bdami_id not in deletions_invalid and bdami_id not in transient_single_invalid and reason != "Not found in Preservica"]

    invalid_ids_str = " ".join(sorted(digbdami_invalid_ids))

    list_logger.info(f"\nINVALID BDAMI IDs in Preservica 'BDAMI':\n{invalid_ids_str}")

    valid_deletion_ids_str = " ".join(sorted(deletions_valid))
    invalid_deletion_ids_str = " ".join(sorted(deletions_invalid))
    
    list_logger.info(f"\n*VALID* IN DELETION FOLDER ({len(deletions_valid)}/{total_pkgs}): \n{valid_deletion_ids_str}")
    list_logger.info(f"\nINVALID IN DELETION FOLDER ({len(deletions_invalid)}/{total_pkgs}): \n{invalid_deletion_ids_str}")

    transient_instances = []
    for bdami_id in all_processed_bdamis:
        all_insts = all_matching_instances.get(bdami_id, []) + all_invalid_instances.get(bdami_id, [])
        for inst in all_insts:
            if inst.get('location') == "Transient Folders":
                transient_instances.append(f"{bdami_id} | UUID: {inst['uuid']} | Ingest Date: {inst['date']}")
                
    list_logger.info(f"\nINSTANCES IN TRANSIENT FOLDERS ({len(transient_instances)}):")
    if transient_instances:
        for t_inst in sorted(transient_instances):
            list_logger.info(f"- {t_inst}")
    else:
        list_logger.info("None")

    list_logger.info(f"\n{'='*60}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nForce quitting.")
        os._exit(1)