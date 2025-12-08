#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Component Server Launcher

WARNING: This server uses pickle for serialization. Only use in trusted networks
(e.g. Kubernetes cluster) as pickle can execute arbitrary code. Do not expose
this server to untrusted networks or the internet.

This script launches the ComponentServer to host remote Predbat components.
"""

import sys
import os
import argparse
import asyncio
import logging
import signal

# Add apps/predbat to path so we can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "apps", "predbat"))

from predbat import THIS_VERSION
from component_server import ComponentServer
from components import COMPONENT_LIST


def get_component_class(component_name):
    """
    Lazy-load a component class by name using importlib to avoid circular import issues.

    Args:
        class_name: Name of the component class (e.g., "OctopusAPI")

    Returns:
        Component class or None if not found
    """

    component_def = COMPONENT_LIST.get(component_name)
    if not component_def:
        return None

    try:
        return component_def["class"]
    except Exception as e:
        logging.error(f"Failed to load component class {component_def['class']}: {e}")
        import traceback

        traceback.print_exc()
        return None


def setup_logging(log_level, log_file=None):
    """
    Setup logging configuration.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional log file path
    """
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=getattr(logging, log_level), format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=handlers)


def main():
    """Main entry point for the component server."""
    parser = argparse.ArgumentParser(description="Predbat Component Server - Remote component execution host")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5053, help="Server port (default: 5053)")
    parser.add_argument("--timeout", type=int, default=1800, help="Component inactivity timeout in seconds (default: 1800)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level (default: INFO)")
    parser.add_argument("--log-file", help="Optional log file path")

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level, args.log_file)
    logger = logging.getLogger(__name__)

    logger.info("=" * 80)
    logger.info(f"Predbat Component Server (Predbat VERSION {THIS_VERSION})")
    logger.info("=" * 80)
    logger.info(f"Host: {args.host}")
    logger.info(f"Port: {args.port}")
    logger.info(f"Timeout: {args.timeout} seconds")
    logger.info(f"Log Level: {args.log_level}")
    if args.log_file:
        logger.info(f"Log File: {args.log_file}")
    logger.info(f"Registered Components: OctopusAPI")
    logger.info("=" * 80)
    logger.warning("WARNING: Using pickle serialization - only use in trusted networks!")
    logger.info("=" * 80)

    # Create server instance with lazy component loader
    server = ComponentServer(timeout=args.timeout, component_loader=get_component_class)  # Lazy loader function

    # Setup signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, initiating shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run server
    async def run_with_shutdown():
        """Run server with shutdown handling."""
        # Start server in background
        server_task = asyncio.create_task(server.run(host=args.host, port=args.port))

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Trigger graceful shutdown
        await server.shutdown()

        # Cancel server task if still running
        if not server_task.done():
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(run_with_shutdown())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Server error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)

    logger.info("Component server stopped")


if __name__ == "__main__":
    main()
