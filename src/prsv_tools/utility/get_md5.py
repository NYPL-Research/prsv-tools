import argparse
import hashlib
import logging
import sys
from pathlib import Path

from prsv_tools.utility.cli import Parser

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)


def extant_file(p: str) -> Path:
    path = Path(p)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"{path} is not a file")

    return path


def calculate_md5(file_path: Path) -> str:
    """Calculates the MD5 checksum of a file."""
    try:
        with open(file_path, 'rb') as f:
            if sys.version_info >= (3, 11):
                return hashlib.file_digest(f, "md5").hexdigest()
            else:
                md5 = hashlib.md5()
                # 8MB chunk size for older python versions
                for chunk in iter(lambda: f.read(8388608), b''):
                    md5.update(chunk)
                return md5.hexdigest()
    except (IOError, OSError) as e:
        logger.error(f"Could not read file {file_path} for hashing: {e}")
        return None

def find_files(pkg_path: Path, folders: list[str]) -> list[Path]:
    """Finds all files in the specified subfolders of the package data directory."""
    files = []
    for folder in folders:
        target_dir = pkg_path / 'data' / folder
        if target_dir.exists() and target_dir.is_dir():
            files.extend([f for f in target_dir.rglob("*") if f.is_file() and not f.name.startswith("._")])
        else:
            logger.warning(f"Directory not found: {target_dir}")
    return files

def main():
    parser = Parser(description='Get MD5 checksums for files in PreservationMasters and/or ServiceCopies subfolders.')
    parser.add_package()
    parser.add_packagedirectory()
    parser.add_argument(
        "--pm", action="store_true", help="Include PreservationMasters subfolder"
    )
    parser.add_argument(
        "--sc", action="store_true", help="Include ServiceCopies subfolder"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include both PreservationMasters and ServiceCopies subfolders",
    )
    parser.add_argument(
        "--file",
        "-f",
        type=extant_file,
        nargs="+",
        help="path to a specific file (can accept multiple)",
    )

    args = parser.parse_args()

    # Determine which folders to scan for packages
    folders = []
    if args.all or args.pm:
        folders.append("PreservationMasters")
    if args.all or args.sc:
        folders.append("ServiceCopies")

    # If packages are provided, subfolder flags are required
    if getattr(args, "packages", None) and not folders:
        parser.error(
            "At least one flag (--pm, --sc, --all) must be provided when using --package or --directory."
        )

    if not getattr(args, "packages", None) and not getattr(args, "file", None):
        logger.warning(
            "No package, directory, or file provided. Use --package, --directory, or --file."
        )
        return

    # Process specific files
    if getattr(args, "file", None):
        for f in args.file:
            checksum = calculate_md5(f)
            if checksum:
                print(f"{checksum}  {f}")

    # Process each package
    if getattr(args, "packages", None):
        for pkg in args.packages:
            logger.info(f"Scanning package: {pkg}")
            files = find_files(pkg, folders)

            if not files:
                logger.info(f"No files found in {folders} for package {pkg.name}")
                continue

            for f in sorted(files):
                checksum = calculate_md5(f)
                if checksum:
                    try:
                        relative_path = f.relative_to(pkg)
                    except ValueError:
                        relative_path = f

                    print(f"{checksum}  {relative_path}")

if __name__ == '__main__':
    main()