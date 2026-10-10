"""Error messages shared by the gateway and the servers."""


def source_not_found_msg() -> str:
    return "Source account not found"


def dest_not_found_msg() -> str:
    return "Destination account not found"


def server_unavailable_msg(server_id: int) -> str:
    return f"Server {server_id} unavailable"
