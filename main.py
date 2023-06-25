import os
import struct
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePath

# Xbox Game Pass for PC savefile extractor

# Running: Just run the script with Python 3 to create ZIP files that contain the save files

# Thanks to @snoozbuster for figuring out the container format at https://github.com/goatfungus/NMSSaveEditor/issues/306

# List of supported Game Pass games and their UWP package names
supported_xgp_apps = {
    "Yakuza 0": "SEGAofAmericaInc.Yakuza0PC_s751p9cej88mt",
    "Yakuza Like a Dragon": "SEGAofAmericaInc.Yazawa_s751p9cej88mt",
    "Octopath Traveller": "39EA002F.FrigateMS_n746a19ndrrjg",
    "Just Cause 4": "39C668CD.JustCause4-BaseGame_r7bfsmp40f67j",
    "Hades": "SupergiantGamesLLC.Hades_q53c1yqmx7pha",
    "Control": "505GAMESS.P.A.ControlPCGP_tefn33qh9azfc",
    "Atomic Heart": "FocusHomeInteractiveSA.579645D26CFD_4hny5m903y3g0",
    "Final Fantasy XV": "39EA002F.FINALFANTASYXVforPC_n746a19ndrrjg"
}


def discover_games():
    found_games = []
    for game_name, pkg_name in supported_xgp_apps.items():
        pkg_path = os.path.expandvars(f"%LOCALAPPDATA%\\Packages\\{pkg_name}")
        if os.path.exists(pkg_path):
            found_games.append(game_name)
    return found_games


def read_utf16_str(f, str_len=None):
    if not str_len:
        str_len = struct.unpack("<i", f.read(4))[0]
    return f.read(str_len * 2).decode("utf-16").rstrip("\0")


def read_containers(pkg_name):
    # Find container dir
    wgs_dir = os.path.expandvars(f"%LOCALAPPDATA%\\Packages\\{pkg_name}\\SystemAppData\\wgs")
    # Get the correct user directory
    dirs = [d for d in os.listdir(wgs_dir) if d != "t"]
    dir_count = len(dirs)
    if dir_count != 1:
        raise Exception(f"Expected one user directory in wgs directory, found {dir_count}")

    containers_dir = os.path.join(wgs_dir, dirs[0])
    containers_idx_path = os.path.join(containers_dir, "containers.index")

    containers = []

    # Read the index file
    with open(containers_idx_path, "rb") as f:
        # Unknown
        f.read(4)

        container_count = struct.unpack("<i", f.read(4))[0]

        # Unknown
        f.read(4)

        store_pkg_name = read_utf16_str(f).split("!Game")[0].split("!Retail")[0].split("!AppChorusShipping")[0].split("!App")[0]

        # Unknown
        f.read(12)
        read_utf16_str(f)

        # Unknown
        f.read(8)

        for _ in range(container_count):
            # Container name
            container_name = read_utf16_str(f)
            # Duplicate of the file name
            read_utf16_str(f)
            # Unknown quoted hex number
            read_utf16_str(f)
            # Container number
            container_num = struct.unpack("B", f.read(1))[0]
            # Unknown
            f.read(4)
            # Read container (folder) GUID
            container_guid = uuid.UUID(bytes_le=f.read(16))
            # Unknown
            f.read(24)

            files = []

            # Read the container file in the container directory
            container_path = os.path.join(containers_dir, container_guid.hex.upper())
            with open(os.path.join(container_path, f"container.{container_num}"), "rb") as cf:
                # Unknown (always 04 00 00 00 ?)
                cf.read(4)
                # Number of files in this container
                file_count = struct.unpack("<i", cf.read(4))[0]
                for _ in range(file_count):
                    # File name, 0x80 (128) bytes UTF-16 = 64 characters
                    file_name = read_utf16_str(cf, 64)
                    # Read file GUID
                    file_guid = uuid.UUID(bytes_le=cf.read(16))
                    # Ignore the copy of the GUID
                    cf.read(16)

                    files.append({
                        "name": file_name,
                        # "guid": file_guid,
                        "path": os.path.join(container_path, file_guid.hex.upper())
                    })

            containers.append({
                "name": container_name,
                "number": container_num,
                # "guid": container_guid,
                "files": files
            })

    return (store_pkg_name, containers)


def get_save_paths(store_pkg_name, containers, temp_dir):
    save_meta = []

    if store_pkg_name in [supported_xgp_apps["Yakuza 0"], supported_xgp_apps["Yakuza Like a Dragon"], supported_xgp_apps["Final Fantasy XV"]]:
        # Handle Yakuza 0, Yakuza Like a Dragon and Final Fantasy XV saves
        # Yakuza 0 uses containers in a "1 container, 1 file" manner (1c1f),
        # where the container includes a file named "data" that is the file named as the container.
        for container in containers:
            fname = container["name"]
            fpath = container["files"][0]["path"]
            save_meta.append((fname, fpath))

    elif store_pkg_name in [supported_xgp_apps["Octopath Traveller"], supported_xgp_apps["Just Cause 4"], supported_xgp_apps["Hades"]]:
        # Handle Octopath Traveller, Just Cause 4 and Hades saves
        # All of these games use containers in a "1 container, n files" manner (1cnf), where there exists only one
        # container that contains all the savefiles.
        # The save files seem to be the same as in the Steam version.
        container = containers[0]
        for c_file in container["files"]:
            save_meta.append((c_file["name"], c_file["path"]))

    elif store_pkg_name == supported_xgp_apps["Control"]:
        # Handle Control saves
        # Control uses container in a "n containers, n files" manner (ncnf),
        # where the container represents a folder that has named files.
        # Epic Games Store (and Steam?) use the same file names, but with a ".chunk" file extension.
        # TODO: Are files named "meta" unnecessary?
        for container in containers:
            path = PurePath(container["name"])

            # Create "--containerDisplayName.chunk" that contains the container name
            # TODO: Does Control _need_ "--containerDisplayName.chunk"?
            temp_container_disp_name_path = Path(temp_dir.name) / f"{container['name']}_--containerDisplayName.chunk"
            with temp_container_disp_name_path.open("w") as f:
                f.write(container["name"])
            save_meta.append((path / "--containerDisplayName.chunk", temp_container_disp_name_path))

            for file in container["files"]:
                save_meta.append((path / f"{file['name']}.chunk", file['path']))
                
    elif store_pkg_name in [supported_xgp_apps["Atomic Heart"]]:
        # Handle Atomic Heart saves
        # Atomic Heart uses containers in a "1 container, 1 file" manner (1c1f),
        # where the container includes a file named "data" that is the file named as the container. All files need to have ".sav" added as an extension
        for container in containers:
            fname = container["name"] + '.sav'
            fpath = container["files"][0]["path"]
            save_meta.append((fname, fpath))

    else:
        raise Exception("Unsupported XGP app \"%s\"" % store_pkg_name)

    return save_meta


def main():
    print("Xbox Game Pass for PC savefile extractor")
    print("========================================")

    # Create tempfile directory
    # Control save files need this, as we need to create files that do not exist in the XGP save data
    temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    # Discover supported games
    found_games = discover_games()

    if len(found_games) == 0:
        print("No supported games installed")
        sys.exit(1)

    print("Installed supported games:")
    for name in found_games:
        print("- %s" % name)

        store_pkg_name, containers = read_containers(supported_xgp_apps[name])

        # Get save file paths
        save_paths = get_save_paths(store_pkg_name, containers, temp_dir)
        print("  Save files:")
        for file_name, _ in save_paths:
            print(f"  - {file_name}")

        # Create a ZIP file
        formatted_game_name = name.replace(" ", "_").replace(":", "_").replace("'", "").lower()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
        zip_name = "%s_%s.zip" % (formatted_game_name, timestamp)
        with zipfile.ZipFile(zip_name, "x") as save_zip:
            for file_name, file_path in save_paths:
                save_zip.write(file_path, arcname=file_name)

        print()
        print("  Save files written to \"%s\"" % zip_name)
        print()

    temp_dir.cleanup()


if __name__ == "__main__":
    main()
