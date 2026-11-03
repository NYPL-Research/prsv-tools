import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import prsv_tools.ingest.move_ami_linted_issues as move_ami


def test_package_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "sys.argv",
        ["script", "--package", str(tmp_path), "--skip-prsv-check"],
    )

    args = move_ami.parse_args()

    assert tmp_path in args.packages
    assert args.skip_prsv_check is True


def test_directory_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    child_dir = tmp_path / "child"
    child_dir.mkdir()

    monkeypatch.setattr(
        "sys.argv",
        ["script", "--directory", str(tmp_path), "--credentials", "test_cred"],
    )

    args = move_ami.parse_args()

    assert child_dir in args.packages
    assert args.credentials == "test_cred"
    assert args.skip_prsv_check is False


def test_credentials_required_if_no_skip_prsv_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "sys.argv",
        ["script", "--package", str(tmp_path)],
    )

    with pytest.raises(SystemExit):
        move_ami.parse_args()


@pytest.fixture
def good_package(tmp_path: Path):
    pkg = tmp_path.joinpath("123456")

    pm_folder = pkg.joinpath("data/PreservationMasters")
    pm_folder.mkdir(parents=True)
    sc_folder = pkg.joinpath("data/ServiceCopies")
    sc_folder.mkdir(parents=True)

    pm_filepath = pm_folder.joinpath("mym_123456_v01_pm.flac")
    pmjson_filepath = pm_folder.joinpath("mym_123456_v01_pm.json")

    sc_filepath = sc_folder.joinpath("mym_123456_v01_sc.mp4")
    scjson_filepath = sc_folder.joinpath("mym_123456_v01_sc.json")

    for file in [
        pm_filepath,
        pmjson_filepath,
        sc_filepath,
        scjson_filepath,
        (pkg / "bagit.txt"),
        (pkg / "manifest-md5.txt"),
    ]:
        file.write_bytes(b"some bytes for object")

    f_metadata = pkg.joinpath("tags")
    f_metadata.mkdir()

    metadata_filepath = f_metadata.joinpath("mym_123456_v01_pm.mkv.xml.gz")
    metadata_filepath.touch()
    metadata_filepath.write_bytes(b"some bytes for metadata")

    return pkg


@pytest.fixture
def tmp_destination(tmp_path: Path):
    tmp_dir = tmp_path.joinpath("destination")
    tmp_dir.mkdir()
    return tmp_dir


def test_set_dir_move_as_expected(tmp_destination: Path, good_package: Path, caplog):
    caplog.set_level(logging.INFO)
    new_folder_name = "create_scs"
    new_dir = tmp_destination / new_folder_name
    new_dir.mkdir(parents=True)

    move_ami.set_dir(good_package, tmp_destination, new_folder_name)

    assert (
        tmp_destination / new_folder_name / good_package.parent.name / good_package.name
    ).exists()

    log_msg = f"MOVED: '{good_package.name}' to '{tmp_destination / new_folder_name / good_package.parent.name}' due to issue: {new_folder_name}"
    assert log_msg in caplog.text


def test_set_dir_move_and_create_dir(tmp_destination: Path, good_package: Path):
    new_folder_name = "create_scs"
    new_dir = tmp_destination / new_folder_name

    move_ami.set_dir(good_package, tmp_destination, new_folder_name)

    assert new_dir.exists()
    assert (
        tmp_destination / new_folder_name / good_package.parent.name / good_package.name
    ).exists()


def test_set_dir_fails_safely_with_missing_target(
    tmp_destination: Path, good_package: Path, caplog
):
    new_folder_name = "create_scs"

    move_ami.set_dir(good_package, tmp_destination / "does_not_exist", new_folder_name)

    log_msg = f"{good_package.name} not moved - '{tmp_destination / 'does_not_exist'}' does not exist."

    assert good_package.exists()
    assert log_msg in caplog.text


def test_main_skip_prsv_check(good_package: Path, tmp_destination: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["script", "--package", str(good_package), "--destination", str(tmp_destination), "--skip-prsv-check"],
    )

    move_ami.main()

    captured = capsys.readouterr()
    assert "Preservica check: SKIPPED (--skip-prsv-check)" in captured.out
    assert "Valid packages (not moved): 1" in captured.out


def test_main_prsv_check_skips_in_prod(good_package: Path, tmp_destination: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["script", "--package", str(good_package), "--destination", str(tmp_destination), "--credentials", "prod-ingest"],
    )

    with patch("prsv_tools.ingest.validate_ami_ingests.get_auth_token", return_value=("fake_token", "v7")):
        with patch("prsv_tools.manage.create_pkg_report.requests_retry_session", return_value=Mock()):
            with patch("prsv_tools.ingest.validate_ami_ingests.get_uuids_for_title", side_effect=lambda t, name, uuid, s: ["some-uuid"] if uuid == move_ami.AMI_UUID_PROD else []):
                move_ami.main()

    captured = capsys.readouterr()
    assert "Packages already in Preservica (skipped lint check): 1" in captured.out
    assert f"- {good_package.name}" in captured.out


def test_main_prsv_check_marks_in_deletion(good_package: Path, tmp_destination: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["script", "--package", str(good_package), "--destination", str(tmp_destination), "--credentials", "prod-ingest"],
    )

    with patch("prsv_tools.ingest.validate_ami_ingests.get_auth_token", return_value=("fake_token", "v7")):
        with patch("prsv_tools.manage.create_pkg_report.requests_retry_session", return_value=Mock()):
            with patch("prsv_tools.ingest.validate_ami_ingests.get_uuids_for_title", side_effect=lambda t, name, uuid, s: ["some-deletion-uuid"] if uuid == move_ami.DELETION_SEARCH_UUID else []):
                move_ami.main()

    captured = capsys.readouterr()
    assert "Packages in Deletion Folder: 1" in captured.out
    assert f"- {good_package.name}" in captured.out
    assert "Valid packages (not moved): 1" in captured.out

