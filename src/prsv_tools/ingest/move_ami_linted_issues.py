import sys
import argparse
import logging
import shutil
import time
from pathlib import Path

import prsv_tools.ingest.lint_ami as prsvlintami
import prsv_tools.utility.cli as prsvcli

logging.basicConfig(level=logging.INFO)

AMI_UUID_PROD = "183a74b5-7247-4fb2-8184-959366bc0cbc"
DELETION_SEARCH_UUID = "836a114b-839a-4af8-a4e1-001f200d6d40"


def parse_args() -> argparse.Namespace:
    parser = prsvcli.Parser()
    parser.add_package()
    parser.add_packagedirectory()
    parser.add_argument(
        "--destination",
        type=prsvcli.extant_dir,
        help="path to a destination directory",
    )
    parser.add_argument(
        "--ignore_regions",
        "-r",
        action='store_true',
        help="ignore packages that have multiple regions",
    )
    parser.add_argument(
        "--credentials", "-c",
        type=str,
        help="prsv credentials to use",
    )
    parser.add_argument(
        "--skip-prsv-check",
        action="store_true",
        help="skip checking Preservica for package existence",
    )
    args = parser.parse_args()

    if not args.skip_prsv_check and not args.credentials:
        parser.error("the following arguments are required: --credentials/-c (unless --skip-prsv-check is used)")

    return args


def set_dir(package: Path, base_dir: Path, new_folder_name: str):
    if not base_dir or not base_dir.exists():
        logging.error(f"{package.name} not moved - '{base_dir}' does not exist.")
        return
    if package.parent.name in ["Audio", "Film", "Video", "CD", "DVD", "Data", "VCD"]:
        new_dir = base_dir / new_folder_name / package.parent.parent.name 
        # # debug input
        # new_dir_input = input(f"Move '{package.name}' to '{new_dir}' for issue: {new_folder_name}? (y/n): ").lower()
        # if new_dir_input != "y":
            # logging.info(f"'{package.name}' not moved.")
            # sys.exit()
    else:
        new_dir = base_dir / new_folder_name / package.parent.name

    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(package), new_dir)
        logging.info(f"MOVED: '{package.name}' to '{new_dir}' due to issue: {new_folder_name}")
    except PermissionError:
        logging.error(f"'{package.name}' not moved - permission error.")
    except shutil.Error as e:
        logging.error(f"'{package.name}' not moved - {e}")


def get_package_status_and_reason(package: Path, ignore_regions: bool = False) -> tuple[str, str | None]:
    if not prsvlintami.data_folder_has_valid_servicecopies_subfolder(package):
        return "INVALID", "create_scs"
    if not prsvlintami.servicecopies_folder_has_media_files(package):
        return "INVALID", "create_scs"
    if not prsvlintami.package_has_valid_name(package):
        return "INVALID", "no_valid_name"
    if not prsvlintami.package_has_valid_subfolder_names(package):
        return "INVALID", "need_valid_subfolder_name"
    if not prsvlintami.data_folder_has_valid_subfolders(package):
        return "INVALID", "need_valid_subfolders"
    if not prsvlintami.data_folder_has_no_empty_folder(package):
        return "INVALID", "empty_folders"
    if not prsvlintami.data_files_are_expected_types(package):
        return "INVALID", "unexpected_file_types"
    if not prsvlintami.tags_folder_is_flat(package):
        return "INVALID", "tags_subfolder"
    if not prsvlintami.tag_file_is_expected_types(package):
        return "INVALID", "tags_unexpected_file_types"
    if not prsvlintami.data_folder_has_no_uncompressed_formats(package):
        return "INVALID", "uncompressed_files"
    if not prsvlintami.data_folder_has_no_part_files(package):
        return "INVALID", "has_parts"
    if not prsvlintami.data_folders_have_at_least_two_files(package):
        return "INVALID", "invalid_file_count"
    if not prsvlintami.package_is_a_bag(package):
        return "INVALID", "not_bagged"
    if not prsvlintami.package_has_no_hidden_file(package):
        return "INVALID", "has_hidden_files"
    if not prsvlintami.package_has_no_zero_bytes_file(package):
        return "INVALID", "0byte_files"

    if not prsvlintami.region_files_used_correctly(package) and not ignore_regions:
        return "INVALID", "multiple_regions"
    elif not prsvlintami.region_files_used_correctly(package) and ignore_regions:
        return "NEEDS_REVIEW", None

    if not prsvlintami.tags_folder_has_one_to_four_files(package):
        return "INVALID", "tags_invalid_file_count"
    if not prsvlintami.data_folder_uses_streams(package):
        return "NEEDS_REVIEW", "has_streams"
    if not prsvlintami.data_folder_has_valid_preservation_masters_folder(package):
        return "INVALID", "missing_pms"
    if not prsvlintami.preservationmasters_folder_has_media_files(package):
        return "INVALID", "missing_pms"

    return "VALID", None


def delete_empty_dir(dir_path: Path):
    if not dir_path.is_dir():
        logging.warning(f"'{dir_path.name}' is not a directory, will not delete.")
        return

    contents = list(dir_path.iterdir())
    
    is_empty = len(contents) == 0
    is_ds_store_only = len(contents) == 1 and contents[0].name == ".DS_Store"

    if is_empty or is_ds_store_only:
        try:
            prompt_msg = f"Directory '{dir_path}' contains only .DS_Store." if is_ds_store_only else f"Directory '{dir_path}' is empty."
            logging.info(f"{prompt_msg} - Deleting directory.")
            if is_ds_store_only:
                contents[0].unlink()

            dir_path.rmdir()
            logging.info(f"Empty directory '{dir_path}' has been deleted.")

            if dir_path.name in ["Audio", "Film", "Video"]:
                try:
                    dir_path.parent.rmdir()
                    logging.info(f"Empty parent directory '{dir_path.parent}' has been deleted.")
                except OSError:
                    pass
        except OSError as e:
            print(f"Error deleting '{dir_path}': {e}")
    else:
        logging.info(f"Directory '{dir_path}' is not empty and will not be deleted.")


def main():
    args = parse_args()
    
    parent_dirs = set(pkg.parent for pkg in args.packages)
    ignore_regions = True if args.ignore_regions else False

    already_in_prsv = []
    in_deletion_folder = []
    valid_pkgs = []
    needs_review_pkgs = []
    moved_pkgs = []

    session = None
    token = None
    
    if not args.skip_prsv_check:
        # if args.packages:
            # sample_pkg = list(args.packages)[0]
            # sample_base = sample_pkg.parent.parent / "_ingested"/ sample_pkg.parent
            # user_response = input(f"Would move ingested packages to subdirectories in: {sample_base}. Continue? (y/n): ")
            # if user_response.lower() != 'y':
            #     sys.exit()

        import prsv_tools.manage.create_pkg_report as create_pkg_report
        from prsv_tools.ingest.validate_ami_ingests import (
            get_auth_token,
            get_uuids_for_title,
        )
        token, _ = get_auth_token(args.credentials)
        token_start_time = time.time()
        session = create_pkg_report.requests_retry_session()

    for pkg in sorted(args.packages):
        ami_id = pkg.name
        if not args.skip_prsv_check:
            if time.time() - token_start_time > 900: # 15 min == 900
                logging.info("Refreshing Preservica auth token to prevent expiration...")
                token, _ = get_auth_token(args.credentials)
                token_start_time = time.time()

            digami_uuids = get_uuids_for_title(token, ami_id, AMI_UUID_PROD, session)
            if digami_uuids:
                # destination: /Parent/_ingested/batchID/
                ingested_dest_dir = pkg.parent.parent / "_ingested" / pkg.parent.name
                logging.info(f"Skipping lint check for '{ami_id}': package exists in Preservica AMI folder. Moving to '{ingested_dest_dir}'")
                
                try:
                    ingested_dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(pkg), ingested_dest_dir)
                    already_in_prsv.append(ami_id)
                except Exception as e:
                    logging.error(f"Failed to move '{pkg.name}' to '{ingested_dest_dir}': {e}")
                
                continue

            deletion_uuids = get_uuids_for_title(token, ami_id, DELETION_SEARCH_UUID, session)
            if deletion_uuids:
                logging.warning(f"'{ami_id}' found in Preservica Deletion Folder, keeping for reingest.")
                in_deletion_folder.append(ami_id)

        status, reason = get_package_status_and_reason(pkg, ignore_regions)

        if status == "VALID":
            logging.info(f"VALID: '{pkg.name}' is valid & has not been moved.")
            valid_pkgs.append(ami_id)
        elif status == "NEEDS_REVIEW":
            logging.warning(f"NEEDS REVIEW: {pkg.name} has streams or multiple regions, but has not been moved.")
            needs_review_pkgs.append(ami_id)
        elif status == "INVALID" and reason is not None:
            set_dir(pkg, args.destination, reason)
            moved_pkgs.append((ami_id, reason))
        else:
            logging.error(f"'{pkg.name}' has not been moved due to no pkg status.")

    print("\nDeleting empty directories:\n")
    for parent_dir in parent_dirs:
        delete_empty_dir(parent_dir)

    print("\nSummary:")
    if not args.skip_prsv_check:
        print(f"  Packages already in Preservica (moved to _ingested): {len(already_in_prsv)}")
        if already_in_prsv:
            for item in sorted(already_in_prsv):
                print(f"    - {item}")
        print(f"  Packages in Deletion Folder: {len(in_deletion_folder)}")
        if in_deletion_folder:
            for item in sorted(in_deletion_folder):
                print(f"    - {item}")
    else:
        print("  Preservica check: SKIPPED (--skip-prsv-check)")

    print(f"  Valid packages (not moved): {len(valid_pkgs)}")
    print(f"  Packages needing review (not moved): {len(needs_review_pkgs)}")
    print(f"  Invalid packages moved: {len(moved_pkgs)}")
    if moved_pkgs:
        for pkg_name, reason in moved_pkgs:
            print(f"    - {pkg_name} -> {reason}")


if __name__ == "__main__":
    main()