from getpass import getpass

from . import control_plane


def main() -> None:
    control_plane.initialize()
    username = input("Owner username: ").strip()
    password = getpass("Owner password (minimum 12 characters): ")
    confirmation = getpass("Confirm owner password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match; no account was created.")
    try:
        created = control_plane.create_owner(username, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not created:
        raise SystemExit("An owner account already exists; no account was created.")
    print("Owner account created.")


if __name__ == "__main__":
    main()
