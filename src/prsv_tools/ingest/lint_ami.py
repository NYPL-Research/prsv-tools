import argparse
import logging
import re
import glob
from datetime import datetime
from pathlib import Path
from typing import Literal

import prsv_tools.utility.cli as prsvcli

LOGGER = logging.getLogger(__name__)


def _configure_logging(log_folder: Path):
    log_fn = datetime.now().strftime("lint_%Y_%m_%d_%H_%M.log")
    log_fpath = log_folder / log_fn
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(levelname)8s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
        handlers=[logging.FileHandler(log_fpath, mode="w"), logging.StreamHandler()],
    )


def parse_args() -> argparse.Namespace:
    """Validate and return command-line args"""
    parser = prsvcli.Parser()
    parser.add_package()
    parser.add_packagedirectory()
    parser.add_logdirectory()
    return parser.parse_args()


def package_has_valid_name(package: Path) -> bool:
    """Top level folder name has to conform to """
    folder_name = package.name
    match = re.fullmatch(r"\d{6,7}", folder_name)
    if match:
        return True
    else:
        LOGGER.error(f"{folder_name} does not conform to AMI ID format")
        return False


def package_has_valid_subfolder_names(package: Path) -> bool:
    """Second level folders must have data and may have tags folder"""
    expected = set(["data", "tags"])
    found = set([x.name for x in package.iterdir() if x.is_dir()])
    if found <= expected and "data" in found:
        return True
    else:
        LOGGER.error(
            f"{package.name} data folders should only be {', '.join(expected)}, found {found}"
        )
        return False


def data_folder_has_valid_subfolders(package: Path) -> bool:
    """Third level folders must have objects and metadata folder"""
    expected = set(
        ["PreservationMasters", "Mezzanines", "EditMasters", "ServiceCopies", "Images"]
    )
    found = set([x.name for x in (package / "data").iterdir() if x.is_dir()])
    if found <= expected:
        return True
    else:
        LOGGER.error(
            f"{package.name} data folders should only be {', '.join(expected)} should have data and tags, found {found}"
        )
        return False


def data_folder_has_valid_servicecopies_subfolder(package: Path) -> bool:
    """Third level folders must include a ServiceCopies folder"""
    if (package / "data" / "ServiceCopies").exists():
        return True
    else:
        LOGGER.error(
            f"{package.name} does not have a ServiceCopies folder, service files should be created"
        )
        return False
    
def servicecopies_folder_has_media_files(package: Path) -> bool:
    """ServiceCopies folder must have media files"""
    servicecopies_path = package / "data" / "ServiceCopies"
    if not servicecopies_path.is_dir():
        return False
    media_file_ls = [
        x for x in servicecopies_path.iterdir() if x.is_file() and x.suffix.lower() in [".mp4", ".m4a"]
    ]
    if not media_file_ls:
        LOGGER.error(f"{package.name} ServiceCopies folder does not have media files")
        return False

    if any("_em" in file.name for file in media_file_ls):
        LOGGER.error(f"{package.name} has '_em' files in ServiceCopies folder.")
        return False

    return True

def data_folder_has_valid_preservation_masters_folder(package: Path) -> bool:
    """Third level folders must include a PreservationMasters folder"""
    if (package / "data" / "PreservationMasters").exists():
        return True
    else:
        LOGGER.error(
            f"{package.name} does not have a PreservationMasters folder, PM files should be created"
        )
        return False

def preservationmasters_folder_has_media_files(package: Path) -> bool:
    """PreservationMasters folder must have media files"""
    preservationmasters_path = package / "data" / "PreservationMasters"
    if not preservationmasters_path.exists():
        return False
    media_file_ls = [
        x for x in preservationmasters_path.iterdir() if x.is_file() and x.suffix.lower() in [".mkv", ".mov", ".flac", ".wav", ".iso", ".dv"]
    ]
    if not media_file_ls:
        LOGGER.error(f"{package.name} PreservationMasters folder does not have media files")
        return False
    else:
        return True

def data_folder_has_no_empty_folder(package: Path) -> bool:
    """The data folder should not have any empty folders"""
    objects_path = package / "data"
    for i in objects_path.rglob("*"):
        if i.is_dir() and not any(i.iterdir()):
            LOGGER.error(f"{package.name} has empty folder in this package: {i.name}")
            return False
    return True


def data_files_are_expected_types(package: Path) -> bool:
    """The data folder can only have media files, json, and carrier photograph(s)"""
    data_path = package / "data"
    data_file_ls = [x for x in data_path.iterdir() if x.is_file()]
    expected = True
    expected_types = [".mkv", ".flac", ".json", ".jpeg", ".jpg", ".dv", ".mov"]
    for file in data_file_ls:
        if not file.suffix.lower() in expected_types and not file.name.startswith("."):
            LOGGER.error(f"{package.name} has unexpected file {file.name}")
            expected = False
    if not expected:
        return False
    else:
        return True


def tags_folder_is_flat(package: Path) -> bool:
    """The tags folder should not have folder structure"""
    metadata_path = package / "tags"
    if not metadata_path.exists():
        return True
    md_dir_ls = [x for x in metadata_path.iterdir() if x.is_dir()]
    if md_dir_ls:
        LOGGER.error(f"{package.name} has unexpected directory: {md_dir_ls}")
        return False
    else:
        return True


def tags_folder_has_one_to_four_files(package: Path) -> bool:
    """The metadata folder should have zero to 3 files"""
    tags_path = package / "tags"
    if not tags_path.exists():
        return True
    md_file_ls = [x for x in tags_path.iterdir() if x.is_file()]
    if len(md_file_ls) > 3:
        LOGGER.warning(
            f"{package.name} has more than four files in the metadata folder: {md_file_ls}"
        )
        return False
    else:
        return True


def tag_file_is_expected_types(package: Path) -> bool:
    """The metadata folder can only have FTK report and/or carrier photograph(s)"""
    tags_path = package / "tags"
    if tags_path.exists():
        md_file_ls = [x for x in tags_path.iterdir() if x.is_file()]
    else:
        return True
    expected = True
    expected_types = [".framemd5", ".gz", ".ssa", ".scc", ".vtt", ".srt", ".txt"]
    for file in md_file_ls:
        if file.suffix.lower() == ".gz":
            parts = file.stem.split(".")
            if "mkv" not in parts and "dv" not in parts:
                LOGGER.warning(
                    f"{package.name} has a gz file of an untracked category: {file.name}"
                )
                expected = False
        if file.suffix.lower() == ".txt" and not file.name.endswith("timecodes.txt"):
            LOGGER.warning(
                f"{package.name} has a txt file of an untracked category: {file.name}"
            )
            expected = False
        if not file.suffix.lower() in expected_types:
            LOGGER.error(f"{package.name} has unexpected file {file.name}")
            expected = False
    if not expected:
        return False
    else:
        return True


def data_folder_has_no_uncompressed_formats(package: Path) -> bool:
    """no wav or mov files should be ingested, transcode first"""
    data_path = package / "data"
    uncompressed_files = [
        x for x in data_path.rglob("*") if x.suffix.lower() in [".mov", ".wav"]
    ]
    # filter out mezzanines
    uncompressed_files = [
        x for x in uncompressed_files if not str(x).endswith("mz.mov")
    ]
    if uncompressed_files:
        LOGGER.error(
            f"{package.name} has uncompressed format files, {uncompressed_files}."
        )
        return False
    else:
        return True


def data_folder_has_no_part_files(package: Path) -> bool:
    """no media file should be a 'part' file, e.g. div_id_v##..p##_"""
    data_path = package / "data"
    part_files = [x for x in data_path.rglob("*") if re.search(r"p\d\d", x.name)]
    if part_files:
        LOGGER.error(f"{package.name} has part files, {part_files}.")
        return False
    else:
        return True


def data_folder_uses_streams(package: Path) -> bool:
    """streams should be flagged to check service file"""
    data_path = package / "data"
    stream_files = [x for x in data_path.rglob("*") if re.search(r"s\d\d", x.name)]

    if stream_files:
        LOGGER.warning(f"{package.name} has streams, {stream_files}.")
        return False
    else:
        return True


def region_files_used_correctly(package: Path) -> bool:
    """media files shouldn't use region for stream"""
    data_path = package / "data"
    high_region_counts = [
        x for x in data_path.rglob("*") if re.search(r"r(0[3-9]|[1-9]\d)", x.name)
    ]
    high_region_counts = [x for x in data_path.rglob("*") if re.search(r"r(0[3-9]|[1-9]\d)", x.name)]

    if high_region_counts:
        LOGGER.error(f"{package.name} has more than 2 regions, {high_region_counts}.")
        return False
    else:
        return True


def data_folders_have_at_least_two_files(package: Path) -> bool:
    """The data folders must have two or more files, which can be in folder(s)"""
    objects_path = package / "data"
    data_folders = [x for x in objects_path.iterdir() if x.is_dir()]
    for folder_path in data_folders:
        if folder_path.name in ["Images", "ServiceCopies"]:
            continue
        data_filepaths = []
        # data_filepaths = [x for x in folder_path.rglob("*") if x.is_file()]
        data_filepaths = glob.glob(str(folder_path / '**/*'), recursive=True) # using glob.glob for symlinks, returns strings not paths
        if len(data_filepaths) < 2:
            LOGGER.error(
                f"{package.name} {folder_path.name} does not have 2 or more files: {data_filepaths}"
            )
            return False
    return True


def package_is_a_bag(package: Path) -> bool:
    """The whole package should be a bag"""
    if not (package / "bagit.txt").exists():
        LOGGER.error(f"{package.name} is not a bag structure")
        return False
    else:
        return True


def package_has_no_hidden_file(package: Path) -> bool:
    """The package should not have any hidden or system file"""
    hidden_ls = [
        h
        for h in package.rglob("*")
        if h.name.startswith(".") or h.name.startswith("Thumbs")
    ]
    if hidden_ls:
        LOGGER.warning(f"{package.name} has hidden or system files {hidden_ls}")
        return False
    else:
        return True


def package_has_no_zero_bytes_file(package: Path) -> bool:
    """The package should not have any zero bytes file"""
    all_file = [f for f in package.rglob("*") if f.is_file()]
    zero_bytes_ls = [f for f in all_file if f.stat().st_size == 0]
    if zero_bytes_ls:
        LOGGER.error(f"{package.name} has zero bytes file {zero_bytes_ls}")
        return False
    else:
        return True

def lint_package(package: Path) -> Literal["valid", "invalid", "needs review"]:
    """Run all linting tests against a package"""
    result = "valid"
    less_strict_tests = [
        tags_folder_has_one_to_four_files,
        package_has_no_hidden_file,
        region_files_used_correctly,
    ]
    for test in less_strict_tests:
        if not test(package):
            result = "needs review"
    strict_tests = [
        package_has_valid_name,
        package_has_valid_subfolder_names,
        data_folder_has_valid_subfolders,
        data_folder_has_valid_servicecopies_subfolder,
        data_folder_has_valid_preservation_masters_folder,
        preservationmasters_folder_has_media_files,
        data_folder_has_no_empty_folder,
        tags_folder_is_flat,
        tag_file_is_expected_types,
        # metadata_FTK_file_has_valid_filename,
        data_folder_has_no_uncompressed_formats,
        data_folder_has_no_part_files,
        data_folder_uses_streams,
        data_folders_have_at_least_two_files,
        package_is_a_bag,
        package_has_no_zero_bytes_file,
        servicecopies_folder_has_media_files,
        data_files_are_expected_types,
        data_folder_uses_streams,
    ]
    for test in strict_tests:
        if not test(package):
            result = "invalid"
    return result


def lint_packages(packages: Path):
    valid = []
    invalid = []
    needs_review = []
    for package in sorted(packages):
        result = lint_package(package)
        LOGGER.info(f"{result}, {package}")
        if result == "valid":
            valid.append(package)
        elif result == "invalid":
            invalid.append(package)
        else:
            needs_review.append(package)

    return invalid, needs_review, valid


def main():
    args = parse_args()
    _configure_logging(args.log_folder)

    invalid, needs_review, valid = lint_packages(args.packages)

    # print(f"\nTotal packages ran: {counter}")
    if valid:
        print(
            f"""
        The following {len(valid)} packages are valid: {", ".join([x.name for x in valid])}"""
        )
    if invalid:
        print(
            f"""
        The following {len(invalid)} packages are invalid: {", ".join([x.name for x in invalid])}"""
        )
    if needs_review:
        print(
            f"""
        The following {len(needs_review)} packages need review.
        They may be passed without change after review: {", ".join([x.name for x in needs_review])}"""
        )


if __name__ == "__main__":
    main()
