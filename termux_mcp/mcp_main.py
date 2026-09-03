import argparse


from . import mcp_server


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="termux-native-mcp",
        description="Native MCP server for Termux (Model Context Protocol, "
                    "spec 2025-06-18). REST daemon 'termux-mcp' is "
                    "separate and unaffected.",
    )
    parser.add_argument(
        "--stdio", action="store_true",
        help="Speak MCP over stdin/stdout instead of HTTP "
             "(for Claude Desktop / Cursor via ssh).")
    parser.add_argument(
        "--port", type=int, default=None,
        help="HTTP listen port (default: $TERMUX_NATIVE_MCP_PORT or 8081).")
    parser.add_argument(
        "--host", type=str, default=None,
        help="Bind address (default: $TERMUX_NATIVE_MCP_HOST or "
             "127.0.0.1). Non-loopback requires an auth token.")
    args = parser.parse_args(argv)

    if args.stdio:
        mcp_server.run_stdio()
    else:
        mcp_server.run_http(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
