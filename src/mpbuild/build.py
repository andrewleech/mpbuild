import os
from typing import Optional, List

from pathlib import Path
import multiprocessing
import subprocess

from rich import print
from rich.panel import Panel
from rich.markdown import Markdown

from . import board_database, find_mpy_root
from .board_database import Board

ARM_BUILD_CONTAINER = "micropython/build-micropython-arm"
BUILD_CONTAINERS = {
    "stm32": ARM_BUILD_CONTAINER,
    "rp2": ARM_BUILD_CONTAINER,
    "nrf": ARM_BUILD_CONTAINER,
    "mimxrt": ARM_BUILD_CONTAINER,
    "renesas-ra": ARM_BUILD_CONTAINER,
    "samd": ARM_BUILD_CONTAINER,
    "esp32": "espressif/idf:v5.2.2",
    "esp8266": "larsks/esp-open-sdk",
    "unix": "gcc:12-bookworm",  # Special, doesn't have boards
}


class MpbuildNotSupportedException(Exception):
    pass


def get_build_container(board: Board, variant: Optional[str] = None) -> str:
    """
    Returns the container to be used for this board/variant.

    Example: board="RPI_PICO" => "micropython/build-micropython-arm"
    Example: board="RPI_PICO", variant="RISCV" => "micropython/build-micropython-rp2350riscv"
    """
    port = board.port

    if port.name == "rp2":
        if variant == "RISCV":
            # Special case: This board supports an ARM core as default
            # and a RISC-V core as a variant
            return "micropython/build-micropython-rp2350riscv"

        # RP2 requires a recent version of gcc
        return "micropython/build-micropython-arm:bookworm"

    try:
        return BUILD_CONTAINERS[port.name]
    except KeyError as e:
        raise MpbuildNotSupportedException(f"{board.name}-{variant}") from e


nprocs = multiprocessing.cpu_count()


def docker_build_cmd(
    board: Board,
    variant: Optional[str] = None,
    extra_args: List[str] = [],
    do_clean: bool = False,
    build_container_override: str | None = None,
    docker_interactive: bool = True,
) -> str:
    """
    Returns the docker-command which will build the firmware.
    """

    port = board.port

    if variant:
        v = board.find_variant(variant)
        if not v:
            raise ValueError(
                f"Variant '{variant}' not found for board '{board.name}': Valid variants are: {[v.name for v in board.variants]}"
            )

    build_container = (
        build_container_override
        if build_container_override
        else get_build_container(board=board, variant=variant)
    )

    variant_param = "BOARD_VARIANT" if board.physical_board else "VARIANT"
    variant_cmd = "" if variant is None else f" {variant_param}={variant}"

    args = " " + " ".join(extra_args)

    make_mpy_cross_cmd = "make -C mpy-cross && "
    update_submodules_cmd = f"make -C ports/{port.name} BOARD={board.name}{variant_cmd} submodules && "

    uid, gid = os.getuid(), os.getgid()

    if do_clean:
        # When cleaning we run with full privs
        uid, gid = 0, 0
        # Don't need to build mpy_cross or update submodules
        make_mpy_cross_cmd = ""
        update_submodules_cmd = ""

    home = os.environ["HOME"]
    mpy_dir = str(port.directory_repo)

    # fmt: off
    build_cmd = (
        f"docker run --rm "
        f"{'-it ' if docker_interactive else ''}"
        f"-v /sys/bus:/sys/bus "                # provides access to USB for deploy
        f"-v /dev:/dev "                        # provides access to USB for deploy
        f"--net=host --privileged "             # provides access to USB for deploy
        f"-v {mpy_dir}:{mpy_dir} -w {mpy_dir} " # mount micropython dir with same path so elf/map paths match host
        f"--user {uid}:{gid} "                  # match running user id so generated files aren't owned by root
        f"-v {home}:{home} -e HOME={home} "     # when changing user id to one not present in container this ensures home is writable
        f"{build_container} "
        f'bash -c "'
        f"git config --global --add safe.directory '*' 2> /dev/null;"
        f'{make_mpy_cross_cmd}'
        f'{update_submodules_cmd}'
        f'make -j {nprocs} -C ports/{port.name} BOARD={board.name}{variant_cmd}{args}"'
    )
    # fmt: on

    return build_cmd


def build_board(
    board: str,
    variant: Optional[str] = None,
    extra_args: List[str] = [],
    build_container_override: Optional[str] = None,
    mpy_dir: str | Path | None = None,
) -> None:
    """
    Build the firmware.

    This command writes to stdout/stderr and may exit the program on failure.
    """
    mpy_dir, _ = find_mpy_root(mpy_dir)
    db = board_database(mpy_dir)
    mpy_dir = db.mpy_root_directory

    if board not in db.boards.keys():
        print("Invalid board")
        raise SystemExit()

    _board = db.boards[board]
    port = _board.port.name

    if variant is not None:
        _variant = _board.find_variant(variant)
        if _variant is None:
            print(f"Invalid variant '{variant}'")
            raise SystemExit()

    if port not in BUILD_CONTAINERS.keys():
        print(f"Sorry, builds are not supported for the {port} port at this time")
        raise SystemExit()

    do_clean = bool(extra_args and extra_args[0].strip() == "clean")
    build_cmd = docker_build_cmd(
        board=_board,
        variant=variant,
        extra_args=extra_args,
        do_clean=do_clean,
        build_container_override=build_container_override,
    )

    title = "Build" if do_clean else "Clean"
    title += f" {port}/{board}" + (f" ({variant})" if variant else "")
    print(Panel(build_cmd, title=title, title_align="left", padding=1))

    proc = subprocess.run(build_cmd, shell=True, check=False)

    if proc.returncode != 0:
        print(f"ERROR: The following command returned {proc.returncode}: {build_cmd}")
        raise SystemExit(proc.returncode)

    # Display deployment markdown for successful builds
    # Note: Only displaying the first deploy file.
    # Q: Are there cases where there's >1? A: Currently, no.
    #    >>> sum([len(b.deploy) for b in db.boards.values()])
    #    166
    #    >>> len(db.boards())
    #    169  # 3x boards are the 'special' boards without deployment instructions.
    if _board.deploy and "clean" not in extra_args and proc.returncode == 0:
        if _board.deploy_filename.is_file():
            print(Panel(Markdown(_board.deploy_filename.read_text())))


def clean_board(
    board: str,
    variant: Optional[str] = None,
    mpy_dir: Optional[str] = None,
) -> None:
    build_board(
        board=board,
        variant=variant,
        mpy_dir=mpy_dir,
        extra_args=["clean"],
    )

def make_command(
    extra_args = None,
    port = None,
    mpy_dir: Optional[str] = None,
) -> None:
    from . import find_mpy_root
    mpy_dir, auto_port = mpy_dir or find_mpy_root()
    
    # Detect port based on command line args, current directory structure, or specified port
    current_port = port
    make_path_from_args = None
    
    # Check for -C argument in extra_args
    if not current_port and extra_args:
        c_index = -1
        for i, arg in enumerate(extra_args):
            if arg == "-C" and i < len(extra_args) - 1:
                c_index = i
                break
        
        # If -C flag found with a path
        if c_index >= 0:
            target_path = extra_args[c_index + 1]
            target_path_obj = Path(target_path)
            
            # Convert to absolute path if relative
            if not target_path_obj.is_absolute():
                target_path_obj = Path.cwd() / target_path_obj
            
            # Check if the path points to a port directory
            ports_path = Path(mpy_dir) / "ports"
            if ports_path in target_path_obj.parents:
                port_parts = target_path_obj.relative_to(ports_path).parts
                if port_parts:
                    current_port = port_parts[0]
                    make_path_from_args = str(target_path_obj.relative_to(mpy_dir))
    
    # If port not detected from args, check current directory
    if not current_port:
        cwd = Path.cwd()
        ports_path = Path(mpy_dir) / "ports"
        
        # Check if we're in a port subdirectory
        if ports_path in cwd.parents:
            # Get immediate child directory of "ports"
            port_parts = cwd.relative_to(ports_path).parts
            if port_parts:
                current_port = port_parts[0]
        
        # Fall back to detected port from find_mpy_root if still not found
        if not current_port:
            current_port = auto_port
    
    if not current_port:
        print(f"Could not determine port from current directory, please specify with --port")
        raise SystemExit()
    
    if current_port not in BUILD_CONTAINERS:
        print(f"Sorry, builds are not supported for the {current_port} port at this time")
        raise SystemExit()
    
    build_container =  BUILD_CONTAINERS[current_port]
    
    # Use the make path from -C arg if found, otherwise determine based on current directory
    if make_path_from_args:
        make_path = make_path_from_args
        # Remove -C and its argument since we're passing it directly to the container
        if extra_args:
            for i, arg in enumerate(extra_args):
                if arg == "-C" and i < len(extra_args) - 1:
                    extra_args = extra_args[:i] + extra_args[i+2:]
                    break
    else:
        # Determine if we need to specify the ports directory or use current directory
        cwd = Path.cwd()
        ports_dir = Path(mpy_dir) / "ports" / current_port
        
        # If we're in a subdirectory of the port, use current directory
        if ports_dir in cwd.parents or ports_dir == cwd:
            make_path = str(cwd.relative_to(mpy_dir))
        else:
            # Otherwise target the specific port
            make_path = f"ports/{current_port}"
    
    args = " " + " ".join(extra_args or [])
    
    nprocs = multiprocessing.cpu_count()
    uid, gid = os.getuid(), os.getgid()
    home = os.environ["HOME"]
    
    # fmt: off
    build_cmd = (
        f"docker run -it --rm "
        f"-v /sys/bus:/sys/bus "                # provides access to USB for deploy
        f"-v /dev:/dev "                        # provides access to USB for deploy
        f"--net=host --privileged "             # provides access to USB for deploy
        f"-v {mpy_dir}:{mpy_dir} -w {mpy_dir} " # mount micropython dir with same path so elf/map paths match host
        f"--user {uid}:{gid} "                  # match running user id so generated files aren't owned by root
        f"-v {home}:{home} -e HOME={home} "     # when changing user id to one not present in container this ensures home is writable
        f"{build_container} "
        f'bash -c "'
        f"git config --global --add safe.directory '*' 2> /dev/null;"
        f'make -j {nprocs} -C {make_path}{args}"'
    )
    # fmt: on
    
    print(Panel(build_cmd, title=f"Make Command: {current_port} ({make_path})", title_align="left", padding=1))
    
    subprocess.run(build_cmd, shell=True)
    